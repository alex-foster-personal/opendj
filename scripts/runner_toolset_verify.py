"""Verify a CI runner host against ci/runner-toolset.yml, as the runner user.

Runs every manifest entry's `verify` command on the host, as the runner user,
under the runner's own job PATH (`<runner-dir>/.path`), and checks each
`provides` executable resolves on that PATH for kinds apt, binary and
toolchain. Read-only: it installs, writes and restarts nothing.

Per entry it prints one of:
  OK        verify exited 0 and its output carries the pinned version
  MISMATCH  verify exited 0 but the version differs (wanted vs got)
  MISSING   verify exited nonzero, or a provided executable is not on PATH
  UNKNOWN   the probe could not run at all (ssh, sudo or timeout failure, or
            no readable runner `.path`: the login PATH is never a stand-in
            for the job PATH). UNKNOWN is never OK.
  INFO      a `provision: job` entry the job installs itself on first use;
            reported for visibility, never a failure.

Version matching (`match`, see ci/runner-toolset.yml): apt entries default to
`min`, compared with dpkg's own ordering, so a newer Ubuntu security revision
(`-4ubuntu3.3` over `-4ubuntu3.2`) is OK and an older one is a MISMATCH.
`exact` and `prefix` compare the whole reported version token, so a
prerelease or build suffix (`1.96.0-nightly`, `v22.23.2-rc.1`) is a MISMATCH.

Exit codes (a stable contract; scripts/ci_runner_host_audit.sh calls this):
  0  every entry OK
  1  at least one MISMATCH or MISSING (takes precedence over UNKNOWN)
  2  at least one UNKNOWN and no MISMATCH/MISSING, or a usage/manifest error

Usage:
  python -m scripts.runner_toolset_verify --host agentbox --user ghrunner
  python -m scripts.runner_toolset_verify --local --user ghrunner   # on the host
  python -m scripts.runner_toolset_verify --host agentbox --only unzip,gh --json

Requirements:
  ✔︎ ✅ R1 each entry is classified OK / MISMATCH / MISSING / UNKNOWN.
    [if] ssh to the host fails [then ⛔️] any entry reports OK or MISSING.
    [if] a verify prints another version [then ⛔️] it reports OK.
    [if] a provided executable is absent from the runner PATH [then ⛔️] OK.
    [if] no readable runner .path exists [then ⛔️] any entry reports OK or MISSING.
    [if] a pinned tool reports the pin plus a `-nightly`/`-rc` suffix [then ⛔️] OK.
  ✔︎ ✅ R2 exit code follows the contract above.
    [if] one entry is UNKNOWN and the rest OK [then ⛔️] exit 0.
    [if] one entry is MISSING [then ⛔️] exit 0 or 2.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass

from scripts.runner_toolset_scan import load_manifest

CFG_TIMEOUT_PER_ENTRY_S = 60
CFG_SSH_TIMEOUT_S = 900
CFG_RUNNER_DIR_GLOBS = "/opt/actions-runner-* /home/*/actions-runner-*"
PATH_KINDS = {"apt", "binary", "toolchain"}
RECORD = "@@RTV@@"


@dataclass
class Result:
    name: str
    kind: str
    status: str
    wanted: str
    got: str


# ----- remote probe -----------------------------------------------------------


def _probe_script(entries: list[dict], runner_dir: str | None) -> str:
    """One bash script that runs every verify and emits a record per entry."""
    lines = [
        "set -u",
        f"rd={shlex.quote(runner_dir) if runner_dir else ''}",
        f'[ -n "$rd" ] || for d in {CFG_RUNNER_DIR_GLOBS}; do'
        ' [ -O "$d" ] && [ -r "$d/.path" ] && rd=$d && break; done',
        # The job PATH is a prerequisite: the invoking shell's PATH says nothing
        # about what a job resolves, so without it nothing is verified (UNKNOWN).
        'if [ -z "$rd" ] || [ ! -r "$rd/.path" ] || [ ! -s "$rd/.path" ]; then',
        f'  echo "no readable, non-empty runner .path (runner dir: ${{rd:-none found under'
        f' {CFG_RUNNER_DIR_GLOBS}}}); refusing to verify against the login PATH" >&2',
        "  exit 96",
        "fi",
        'export PATH="$(cat "$rd/.path")"',
        f'echo "{RECORD} PATHSRC $rd"',
        "cd ~ || exit 97",
    ]
    for entry in entries:
        lines += _probe_lines(entry)
    lines.append(f'echo "{RECORD} END"')
    return "\n".join(lines) + "\n"


def _probe_lines(entry: dict) -> list[str]:
    name = entry["name"]
    verify = base64.b64encode(entry["verify"].encode()).decode()
    run = (
        f"out=$(timeout {CFG_TIMEOUT_PER_ENTRY_S} bash -o pipefail -c "
        f'"$(echo {verify} | base64 -d)" 2>&1); rc=$?; '
        f'echo "{RECORD} V {name} $rc $(printf %s "$out" | head -c 2000 | base64 -w0)"'
    )
    on_path = entry.get("provides", []) if entry["kind"] in PATH_KINDS else []
    return [run] + [
        f'command -v {shlex.quote(exe)} >/dev/null 2>&1 || echo "{RECORD} P {name} {exe}"'
        for exe in on_path
    ]


def _run_probe(script: str, host: str | None, user: str) -> tuple[int, str, str]:
    inner = ["sudo", "-n", "-H", "-u", user, "bash", "-s"]
    argv = (
        inner
        if host is None
        else ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", host, shlex.join(inner)]
    )
    try:
        proc = subprocess.run(
            argv,
            input=script,
            capture_output=True,
            text=True,
            timeout=CFG_SSH_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"probe timed out after {CFG_SSH_TIMEOUT_S}s"
    return proc.returncode, proc.stdout, proc.stderr


# ----- classification ---------------------------------------------------------


def default_match(entry: dict) -> str:
    """apt pins are floors: a newer Ubuntu security revision must never read as drift."""
    return entry.get("match", "min" if entry["kind"] == "apt" else "exact")


# What may follow a whole version token: anything but more version. A letter,
# `-`, `+` or `~` starts a prerelease or build suffix (`1.96.0-nightly`,
# `v22.23.2-rc.1`, `3.11.16rc1`), which is a different toolchain, not the pin.
VERSION_END = r"(?![\w+~-]|\.\w)"


def _version_matches(version: str, match: str, output: str) -> bool:
    if match == "exact":
        return re.search(rf"(?<![\d.]){re.escape(version)}{VERSION_END}", output) is not None
    if match == "prefix":
        pattern = rf"(?<![\d.]){re.escape(version)}(\.\d+)*{VERSION_END}"
        return re.search(pattern, output) is not None
    if match == "min":
        found = re.search(r"(?<![\w.])(?:\d+:)?\d[\w.+~:-]*", output)
        return found is not None and dpkg_compare(found.group(0), version) >= 0
    raise ValueError(f"unknown match mode {match!r}")


# ----- Debian version ordering (dpkg's verrevcmp, deb-version(7)) ----------------


def dpkg_compare(a: str, b: str) -> int:
    """-1, 0 or 1 as `a` sorts before, equal to or after `b`, exactly as dpkg orders them."""
    epoch_a, upstream_a, revision_a = _split_debian(a)
    epoch_b, upstream_b, revision_b = _split_debian(b)
    order = (
        (epoch_a > epoch_b) - (epoch_a < epoch_b)
        or _verrevcmp(upstream_a, upstream_b)
        or _verrevcmp(revision_a, revision_b)
    )
    return (order > 0) - (order < 0)


def _split_debian(version: str) -> tuple[int, str, str]:
    """epoch, upstream, revision (a missing revision compares as "0")."""
    epoch, _, rest = version.partition(":") if ":" in version else ("0", "", version)
    upstream, _, revision = rest.rpartition("-") if "-" in rest else (rest, "", "0")
    return int(epoch), upstream, revision


def _char_order(ch: str) -> int:
    if ch == "~":
        return -1
    if ch.isalpha():
        return ord(ch)
    return ord(ch) + 256


def _verrevcmp(a: str, b: str) -> int:
    """Alternate non-digit runs (by _char_order, `~` first) and digit runs (numerically)."""
    while a or b:
        text_a, a = _leading(a, digits=False)
        text_b, b = _leading(b, digits=False)
        width = max(len(text_a), len(text_b))
        for x, y in zip(text_a.ljust(width, "\0"), text_b.ljust(width, "\0"), strict=True):
            cx = 0 if x == "\0" else _char_order(x)
            cy = 0 if y == "\0" else _char_order(y)
            if cx != cy:
                return cx - cy
        num_a, a = _leading(a, digits=True)
        num_b, b = _leading(b, digits=True)
        if int(num_a or 0) != int(num_b or 0):
            return int(num_a or 0) - int(num_b or 0)
    return 0


def _leading(text: str, digits: bool) -> tuple[str, str]:
    n = 0
    while n < len(text) and text[n].isdigit() == digits:
        n += 1
    return text[:n], text[n:]


@dataclass
class _Records:
    verified: dict[str, tuple[int, str]]
    off_path: dict[str, list[str]]
    complete: bool


def _parse_records(stdout: str) -> _Records:
    records = _Records(verified={}, off_path={}, complete=False)
    for line in stdout.splitlines():
        parts = line.split(" ")
        if parts[0] != RECORD or len(parts) < 2:
            continue
        if parts[1] == "V":
            payload = parts[4] if len(parts) > 4 else ""
            output = base64.b64decode(payload).decode(errors="replace")
            records.verified[parts[2]] = (int(parts[3]), output)
        elif parts[1] == "P":
            records.off_path.setdefault(parts[2], []).append(parts[3])
        elif parts[1] == "END":
            records.complete = True
    return records


def _classify_one(entry: dict, records: _Records, unreached: str) -> tuple[str, str]:
    """(status, what was seen) for one entry."""
    name = entry["name"]
    if name not in records.verified or not records.complete:
        return "UNKNOWN", unreached
    code, output = records.verified[name]
    first = output.strip().splitlines()[0] if output.strip() else "(no output)"
    if entry.get("provision") == "job":
        return "INFO", f"installed by the job on first use; host verify exit {code}: {first}"
    if code in {124, 137}:
        return "UNKNOWN", f"verify timed out: {first}"
    if code != 0:
        return "MISSING", f"verify exit {code}: {first}"
    if name in records.off_path:
        return "MISSING", f"not on runner PATH: {', '.join(records.off_path[name])}"
    if _version_matches(entry["version"], default_match(entry), output):
        return "OK", first
    return "MISMATCH", first


def classify(entries: list[dict], rc: int, stdout: str, stderr: str) -> list[Result]:
    """Turn the probe's records into one Result per entry."""
    records = _parse_records(stdout)
    unreached = stderr.strip().splitlines()[-1] if stderr.strip() else f"probe exit {rc}, no record"
    results = []
    for entry in entries:
        status, got = _classify_one(entry, records, unreached)
        results.append(Result(entry["name"], entry["kind"], status, entry["version"], got))
    return results


def exit_code(results: list[Result]) -> int:
    statuses = {r.status for r in results}
    if statuses & {"MISMATCH", "MISSING"}:
        return 1
    if "UNKNOWN" in statuses or not results:
        return 2
    return 0


# ----- CLI --------------------------------------------------------------------


def _select_entries(only: str | None) -> list[dict]:
    """Manifest entries, narrowed by --only; raises ValueError on an unknown name."""
    entries = load_manifest()["entries"]
    if not only:
        return entries
    wanted = set(only.split(","))
    unknown = wanted - {e["name"] for e in entries}
    if unknown:
        raise ValueError(f"not in manifest: {sorted(unknown)}")
    return [e for e in entries if e["name"] in wanted]


def _print_report(results: list[Result], header: dict[str, str], as_json: bool) -> None:
    if as_json:
        print(json.dumps({**header, "results": [asdict(r) for r in results]}, indent=2))
        return
    print(" ".join(f"{k}={v}" for k, v in header.items()))
    for r in results:
        detail = r.got if r.status == "OK" else f"wanted {r.wanted}, got: {r.got}"
        print(f"{r.status:8} {r.kind:13} {r.name:36} {detail}")
    statuses = ("OK", "MISMATCH", "MISSING", "UNKNOWN", "INFO")
    counts = " ".join(f"{s}={sum(r.status == s for r in results)}" for s in statuses)
    print(f"{counts} total={len(results)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--host", help="ssh alias of the runner host")
    target.add_argument("--local", action="store_true", help="probe this machine")
    parser.add_argument("--user", required=True, help="runner user, e.g. ghrunner")
    parser.add_argument(
        "--runner-dir",
        help="runner dir whose .path is the job PATH (default: first one the user owns)",
    )
    parser.add_argument("--only", help="comma-separated entry names")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        entries = _select_entries(args.only)
    except (OSError, TypeError, ValueError) as exc:
        print(f"[ERROR] manifest: {exc}", file=sys.stderr)
        return 2
    script = _probe_script(entries, args.runner_dir)
    rc, stdout, stderr = _run_probe(script, None if args.local else args.host, args.user)
    results = classify(entries, rc, stdout, stderr)
    path_src = next(
        (ln.split(" ", 2)[2] for ln in stdout.splitlines() if ln.startswith(f"{RECORD} PATHSRC")),
        "UNKNOWN",
    )
    header = {"host": args.host or "local", "user": args.user, "path_from": path_src}
    _print_report(results, header, args.json)
    return exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
