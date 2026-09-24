"""Generate the fleet process registry: ``docs/ops/process-registry.md``.

Issue #2542: every process/service/timer/launchd agent we own is named
``opendj-<area>-<part>`` (systemd, process titles) or
``com.opendj.<area>-<part>`` (launchd), and listed in one registry so a
permission prompt, Activity Monitor, ``ps``, or a scheduler listing can
always be traced back to "what is this and whose is it".

A hand-written registry rots within a week (the maintainer, issue #2542 discussion).
So this file does not hand-list processes: it QUERIES every scheduler on
every host live, classifies what it finds against the small ownership
table in ``process_registry_sources.py``, and renders the registry from
that live inventory. Re-run it to refresh the snapshot; do not hand-edit
``docs/ops/process-registry.md`` or ``docs/ops/process-registry.json``.

Usage::

    python -m scripts.process_registry_gen                 # write both files
    python -m scripts.process_registry_gen --hosts silver,nucbox-wsl
    python -m scripts.process_registry_gen --stdout-only    # print, don't write

Each host is queried independently; an unreachable host is recorded as
UNREACHABLE with its error, never silently skipped -- a skipped host would
otherwise look identical to a host with zero owned processes, which is the
absence-of-a-bad-thing trap ``.claude/rules/verification.md`` names.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from scripts.process_registry_sources import (
    GITHUB_ACTIONS_HOST,
    HOSTS,
    Host,
    HostResult,
    HostUnreachableError,
    ProcessUnit,
    SchedulerKind,
    _run,
    _ssh,
    classify,
    naming_status,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "ops" / "process-registry.md"
JSON_PATH = REPO_ROOT / "docs" / "ops" / "process-registry.json"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

GENERATOR = "scripts/process_registry_gen.py"

# Names worth a second look even though no ownership rule matched: this is
# exactly the issue #2542 "2.1.270" case (an ambiguous permission-prompt
# name that might be ours). A plain com.apple.* framework helper is not.
_NEAR_MISS_RE = re.compile(r"af|opendj|music|claude|anthropic", re.IGNORECASE)


# ------------------------------------------------------------ collectors


def _make_unit(
    host: str,
    scheduler: SchedulerKind,
    unit: str,
    schedule: str,
    state: str,
    last_exit: str,
    command: str,
) -> ProcessUnit:
    owned, area, purpose = classify(host, unit)
    return ProcessUnit(
        host=host,
        scheduler=scheduler,
        unit=unit,
        schedule=schedule,
        state=state,
        last_exit=last_exit,
        command=command,
        owned=owned,
        area=area,
        purpose=purpose,
        naming=naming_status(scheduler, unit),
    )


_LAUNCHD_SCHEDULE_NOTE = "see plist (StartInterval/StartCalendarInterval, not parsed here)"


def _parse_launchctl_list(host: str, raw: str) -> list[ProcessUnit]:
    units: list[ProcessUnit] = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or parts[0] in ("PID", "HOST_OK"):
            continue
        pid, status, label = parts
        state = "running" if pid not in ("-", "") else "not running"
        last_exit = status if status not in ("-", "") else "0"
        units.append(
            _make_unit(
                host,
                SchedulerKind.LAUNCHD,
                label,
                schedule=_LAUNCHD_SCHEDULE_NOTE,
                state=state,
                last_exit=last_exit,
                command="launchctl list",
            )
        )
    return units


def collect_launchd(host: Host) -> HostResult:
    cmd = "date -u +%Y-%m-%dT%H:%M:%SZ; launchctl list 2>&1"
    raw = _run(["bash", "-lc", cmd]) if host.ssh_alias is None else _ssh(host.ssh_alias, cmd)
    return HostResult(host=host.name, reachable=True, units=_parse_launchctl_list(host.name, raw))


_UNKNOWN_FETCHED_SEPARATELY = "UNKNOWN (fetched separately)"
_UNKNOWN_NOT_RETURNED = "UNKNOWN (not returned by systemctl show)"


def _parse_systemd_timers(
    host_name: str, scheduler: SchedulerKind, raw: str, command_label: str
) -> tuple[list[ProcessUnit], list[str]]:
    """Parse ``systemctl list-timers`` output into units + their service names."""
    units: list[ProcessUnit] = []
    service_names: list[str] = []
    for line in raw.splitlines():
        if ".timer" not in line or line.startswith(("NEXT", "HOST_OK")):
            continue
        cols = line.split()
        # Last two columns are "<unit>.timer <unit>.service"; NEXT/LAST are
        # multi-word ("Wed 2026-09-16 06:32:53 BST"), so anchor from the end.
        if len(cols) < 2:
            continue
        activates = cols[-1]
        base = cols[-2].removesuffix(".timer")
        service_names.append(base)
        schedule = " ".join(cols[:-2]) or "UNKNOWN (could not parse list-timers row)"
        units.append(
            _make_unit(
                host_name,
                scheduler,
                base,
                schedule=schedule,
                state=_UNKNOWN_FETCHED_SEPARATELY,
                last_exit=_UNKNOWN_FETCHED_SEPARATELY,
                command=f"{command_label} -> {activates}",
            )
        )
    return units, service_names


def _fill_systemd_status(
    ssh_alias: str, units: list[ProcessUnit], service_names: list[str], show_cmd_prefix: str
) -> None:
    """Fetch state/last-result for units in one batched ``systemctl show``."""
    if not service_names:
        return
    names = " ".join(f"{n}.service" for n in service_names)
    show = _ssh(ssh_alias, f"{show_cmd_prefix} {names} -p Id,Result,ActiveState --no-pager 2>&1")
    by_id: dict[str, dict[str, str]] = {}
    for block in show.split("\n\n"):
        fields = dict(kv.split("=", 1) for kv in block.splitlines() if "=" in kv)
        if "Id" in fields:
            by_id[fields["Id"].removesuffix(".service")] = fields
    for u in units:
        f = by_id.get(u.unit, {})
        u.state = f.get("ActiveState", _UNKNOWN_NOT_RETURNED)
        u.last_exit = f.get("Result", _UNKNOWN_NOT_RETURNED)


def collect_systemd_user(host: Host) -> HostResult:
    assert host.ssh_alias
    timers_raw = _ssh(host.ssh_alias, "systemctl --user list-timers --all --no-pager --plain 2>&1")
    units, service_names = _parse_systemd_timers(
        host.name, SchedulerKind.SYSTEMD_USER, timers_raw, "systemd --user timer"
    )
    _fill_systemd_status(host.ssh_alias, units, service_names, "systemctl --user show")
    return HostResult(host=host.name, reachable=True, units=units)


def _cron_unit_name(command: str) -> str:
    """Basename of the script/binary a cron line runs.

    The last token that looks like a real path: has a "/", isn't a flag,
    and isn't a shell redirect target like ">/dev/null" or "2>&1" (both of
    which also contain "/" and would otherwise win as "the last path-like
    token").
    """
    unit_name = command
    for tok in command.split():
        tok_clean = tok.lstrip("<>&0123456789")
        looks_like_path = "/" in tok_clean and not tok_clean.startswith("-")
        if looks_like_path and tok_clean != "/dev/null":
            unit_name = tok_clean.split("/")[-1]
    return unit_name


def _parse_crontab(host_name: str, raw: str) -> list[ProcessUnit]:
    units: list[ProcessUnit] = []
    seen: set[tuple[str, str]] = set()
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "no crontab" in line or line == "---ROOTCRON---":
            continue
        cols = line.split(None, 5)
        if len(cols) < 6:
            continue
        schedule = " ".join(cols[:5])
        command = cols[5]
        if (schedule, command) in seen:
            continue  # same job read twice (user crontab == root crontab on this host)
        seen.add((schedule, command))
        units.append(
            _make_unit(
                host_name,
                SchedulerKind.CRON,
                _cron_unit_name(command),
                schedule=schedule,
                state="scheduled (cron)",
                last_exit="UNKNOWN (cron does not record exit status)",
                command=command,
            )
        )
    return units


def collect_systemd_system(host: Host) -> HostResult:
    assert host.ssh_alias
    user_units = collect_systemd_user(host).units
    for u in user_units:
        u.command += " (systemctl --user)"

    sys_raw = _ssh(host.ssh_alias, "sudo systemctl list-timers --all --no-pager --plain 2>&1")
    sys_units, service_names = _parse_systemd_timers(
        host.name, SchedulerKind.SYSTEMD_SYSTEM, sys_raw, "systemctl (system) timer"
    )
    _fill_systemd_status(host.ssh_alias, sys_units, service_names, "sudo systemctl show")

    # crontab (root, for agentbox's codex-transcript-cleanup)
    cron_raw = _ssh(host.ssh_alias, "crontab -l 2>&1; echo ---ROOTCRON---; sudo crontab -l 2>&1")
    cron_units = _parse_crontab(host.name, cron_raw)

    return HostResult(host=host.name, reachable=True, units=user_units + sys_units + cron_units)


def collect_windows_tasks(host: Host) -> HostResult:
    assert host.ssh_alias
    # $_ inside a double-quoted -Command string is expanded by the remote
    # git-bash shell before PowerShell ever sees it (the MSYS trap named in
    # the issue). Select-Object/Format-Table with no Where-Object avoids the
    # variable entirely; filtering happens locally instead.
    raw = _ssh(
        host.ssh_alias,
        "powershell.exe -NoProfile -Command "
        '"Get-ScheduledTask | Select-Object TaskName,State,TaskPath | Format-Table -AutoSize"',
        timeout=40,
    )
    units: list[ProcessUnit] = []
    for line in raw.splitlines():
        if line.startswith(("**", "TaskName", "--------", "HOST_OK")) or not line.strip():
            continue
        # Fixed-width table: last whitespace-delimited column is TaskPath,
        # the one before it is State, everything else is TaskName.
        cols = line.split()
        if len(cols) < 2:
            continue
        state = cols[-2] if cols[-2] in ("Ready", "Running", "Disabled") else None
        if state is None:
            continue
        task_name = line[: line.index(state)].strip()
        units.append(
            _make_unit(
                host.name,
                SchedulerKind.WINDOWS_TASK,
                task_name,
                schedule="UNKNOWN (Get-ScheduledTaskInfo not queried this pass)",
                state=state,
                last_exit="UNKNOWN (Get-ScheduledTaskInfo not queried this pass)",
                command="Get-ScheduledTask",
            )
        )
    return HostResult(host=host.name, reachable=True, units=units)


def _extract_cron_schedules(workflow_text: str) -> list[str]:
    in_schedule_block = False
    crons: list[str] = []
    for line in workflow_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped == "schedule:":
            in_schedule_block = True
            continue
        if in_schedule_block:
            if stripped.startswith("- cron:"):
                crons.append(stripped.split("cron:", 1)[1].strip().strip("\"'"))
                continue
            if stripped and not stripped.startswith("-"):
                in_schedule_block = False
    return crons


def collect_github_actions(_host: Host) -> HostResult:
    units = [
        _make_unit(
            "github-actions",
            SchedulerKind.GITHUB_ACTIONS,
            wf.stem,
            schedule=cron,
            state="enabled (workflow file present on main)",
            last_exit="UNKNOWN (query `gh run list` for real run history)",
            command=str(wf.relative_to(REPO_ROOT)),
        )
        for wf in sorted(WORKFLOWS_DIR.glob("*.yml"))
        for cron in _extract_cron_schedules(wf.read_text(encoding="utf-8"))
    ]
    return HostResult(host="github-actions", reachable=True, units=units)


COLLECTORS = {
    SchedulerKind.LAUNCHD: collect_launchd,
    SchedulerKind.SYSTEMD_USER: collect_systemd_user,
    SchedulerKind.SYSTEMD_SYSTEM: collect_systemd_system,
    SchedulerKind.WINDOWS_TASK: collect_windows_tasks,
}


def collect_host(host: Host) -> HostResult:
    try:
        return COLLECTORS[host.scheduler](host)
    except HostUnreachableError as exc:
        return HostResult(host=host.name, reachable=False, error=str(exc))


def collect_all(hosts: list[Host]) -> list[HostResult]:
    results = [collect_host(h) for h in hosts]
    results.append(collect_github_actions(GITHUB_ACTIONS_HOST))
    return results


def _load_previous() -> dict[str, dict]:
    """Map host -> previous host block from the last committed snapshot."""
    if not JSON_PATH.exists():
        return {}
    try:
        doc = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {h["host"]: h for h in doc.get("hosts", [])}


def carry_forward_unqueried_host(prev_block: dict) -> dict:
    """A --hosts filter narrower than the full fleet: carry an untouched host
    forward rather than silently dropping it, WITHOUT discarding its real
    reachable/error/stale_as_of (found reviewing PR #3827's own scoped
    regen, which replaced four hosts' real ssh/DNS errors with a generic
    "not queried" string, and flipped a fifth (agentbox) from
    reachable=True with real data to reachable=False with
    stale_as_of=None -- "unreachable and we don't even know when it last
    worked", strictly less informative than either true state, and exactly
    the absence-of-a-bad-thing trap .claude/rules/verification.md warns
    about). A host not queried this pass is neither confirmed reachable nor
    newly unreachable -- it is unmeasured, so its last REAL measurement
    must survive, tagged as not re-verified rather than replaced.
    """
    not_queried_note = "not queried this pass (--hosts filter)"
    if prev_block.get("reachable"):
        stale_as_of = prev_block.get("generated_at_utc_of_block")
        error = not_queried_note
    else:
        stale_as_of = prev_block.get("stale_as_of") or prev_block.get(
            "generated_at_utc_of_block"
        )
        prev_error = prev_block.get("error")
        error = (
            f"{not_queried_note} -- last known: {prev_error}" if prev_error else not_queried_note
        )
    return {**prev_block, "reachable": False, "error": error, "stale_as_of": stale_as_of}


def merge_with_previous(results: list[HostResult], previous: dict[str, dict]) -> list[dict]:
    """Fill in a stale-but-labeled block for any host unreachable THIS pass.

    An unreachable host must never render as "zero owned units" -- that is
    indistinguishable from a host that is genuinely clean, which is the
    absence-of-a-bad-thing trap this whole design exists to avoid. Instead
    it keeps the last known-good snapshot for that host, explicitly marked
    stale with the timestamp it actually came from.
    """
    merged: list[dict] = []
    for r in results:
        if r.reachable:
            merged.append(
                {
                    "host": r.host,
                    "reachable": True,
                    "error": None,
                    "stale_as_of": None,
                    "units": [u.to_json() for u in r.units],
                }
            )
            continue
        prev = previous.get(r.host)
        merged.append(
            {
                "host": r.host,
                "reachable": False,
                "error": r.error,
                "stale_as_of": prev.get("generated_at_utc_of_block") if prev else None,
                "units": prev["units"] if prev else [],
            }
        )
    return merged


# -------------------------------------------------------------- render


def render_markdown(blocks: list[dict], generated_at: str) -> str:
    lines = [
        "# Fleet process registry",
        "",
        f"GENERATED by `{GENERATOR}` at {generated_at}. Do not hand-edit --",
        "re-run `python -m scripts.process_registry_gen` to refresh. The",
        "machine-readable twin is `docs/ops/process-registry.json`; this file",
        "is rendered from it.",
        "",
        "Issue #2542. Naming convention: `opendj-<area>-<part>` for systemd",
        "units and process titles, `com.opendj.<area>-<part>` for launchd",
        "labels. Drift check: `python -m scripts.process_registry_check`.",
        "",
        "Scope note: this registry covers processes/units/timers that serve",
        "the music-dj-tools / Open DJ product and its build fleet. the maintainer's own",
        "personal automation (trip planners, KVM, comms bridges, and similar",
        "`com.af.*` / Windows Task Scheduler entries unrelated to this repo)",
        'is out of scope by design and listed in "Not ours" below so nobody',
        'chases it here again -- see issue #2542\'s "2.1.270" case.',
        "",
    ]

    total_row = [
        "| Host | Reachable | Owned units | Naming violations | Unknown purpose |",
        "|---|---|---|---|---|",
    ]
    for b in blocks:
        owned = [u for u in b["units"] if u["owned"]]
        violations = [u for u in owned if u["naming"] == "violation"]
        unknown = [u for u in owned if str(u["purpose"]).startswith("UNKNOWN")]
        reach = "yes" if b["reachable"] else f"NO -- {b['error']}"
        total_row.append(
            f"| {b['host']} | {reach} | {len(owned)} | {len(violations)} | {len(unknown)} |"
        )
    lines += [*total_row, ""]

    for b in blocks:
        lines.append(f"## {b['host']}")
        lines.append("")
        if not b["reachable"]:
            lines.append(f"**UNREACHABLE this pass** -- {b['error']}")
            lines.append("")
            if b["units"]:
                lines.append(
                    f"Units below are STALE, carried over from the last successful "
                    f"pass ({b.get('stale_as_of') or 'timestamp unknown'}), not "
                    "re-verified this run."
                )
            else:
                lines.append(
                    "No previous snapshot exists for this host either -- treat as "
                    "UNKNOWN, not as zero owned processes."
                )
            lines.append("")
        owned = [u for u in b["units"] if u["owned"]]
        if not owned:
            lines.append("_No owned units found._" if b["reachable"] else "_No prior data._")
            lines.append("")
        else:
            lines.append(
                "| Unit | Scheduler | Schedule | State | Last result | Area | Purpose | Naming |"
            )
            lines.append("|---|---|---|---|---|---|---|---|")
            for u in sorted(owned, key=lambda x: (x["area"], x["unit"])):
                lines.append(
                    f"| `{u['unit']}` | {u['scheduler']} | {u['schedule']} | {u['state']} | "
                    f"{u['last_exit']} | {u['area']} | {u['purpose']} | {u['naming']} |"
                )
            lines.append("")
        not_ours = [u for u in b["units"] if not u["owned"]]
        near_miss = [u for u in not_ours if _NEAR_MISS_RE.search(u["unit"])]
        if not_ours:
            lines.append(
                f"<details><summary>Not ours on {b['host']} ({len(not_ours)} total; "
                f"{len(near_miss)} shown -- names close enough to be worth a second "
                'look, per issue #2542\'s "2.1.270" case)</summary>'
            )
            lines.append("")
            if near_miss:
                lines.append("| Unit | Scheduler | Reason |")
                lines.append("|---|---|---|")
                for u in sorted(near_miss, key=lambda x: x["unit"]):
                    lines.append(f"| `{u['unit']}` | {u['scheduler']} | {u['purpose']} |")
                lines.append("")
            lines.append(
                f"The remaining {len(not_ours) - len(near_miss)} are stock OS/vendor "
                "processes with no name overlap with `af`/`opendj`/`music`/`claude`/"
                "`anthropic`; full list is in `process-registry.json`, omitted here "
                "for readability."
            )
            lines.append("")
            lines.append("</details>")
            lines.append("")

    return "\n".join(lines) + "\n"


def build_doc(blocks: list[dict], generated_at: str) -> dict:
    return {"generated_by": GENERATOR, "generated_at_utc": generated_at, "hosts": blocks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hosts", help="comma-separated host names to query (default: all)")
    parser.add_argument("--stdout-only", action="store_true", help="print markdown, write nothing")
    args = parser.parse_args(argv)

    hosts = HOSTS
    if args.hosts:
        wanted = set(args.hosts.split(","))
        hosts = [h for h in HOSTS if h.name in wanted]

    generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    results = collect_all(hosts)
    previous = _load_previous()
    blocks = merge_with_previous(results, previous)
    queried_names = {b["host"] for b in blocks}
    for host_name, prev_block in previous.items():
        if host_name in queried_names:
            continue
        # A --hosts filter narrower than the full fleet: carry the untouched
        # host forward rather than silently dropping it from the snapshot.
        blocks.append(carry_forward_unqueried_host(prev_block))
    for b in blocks:
        if b["reachable"]:
            b["generated_at_utc_of_block"] = generated_at
        else:
            b.setdefault("generated_at_utc_of_block", None)

    md = render_markdown(blocks, generated_at)
    doc = build_doc(blocks, generated_at)

    if args.stdout_only:
        print(md)
        return 0

    DOC_PATH.write_text(md, encoding="utf-8")
    JSON_PATH.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    unreachable = [b["host"] for b in blocks if not b["reachable"]]
    print(f"wrote {DOC_PATH} and {JSON_PATH}")
    if unreachable:
        print(
            "UNREACHABLE this pass (previous snapshot kept, marked stale): "
            + ", ".join(unreachable),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
