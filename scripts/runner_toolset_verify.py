"""Verify a CI runner host against ci/runner-toolset.yml, as the runner user.

Runs every manifest entry's `verify` command on the host, as the runner user,
under each configured runner's own job PATH (for every runner dir holding
`.runner`: its `.env` `PATH=` when set, which the Listener loads and which
wins, else `<runner-dir>/.path`; runners sharing a job PATH are probed once), and
checks each `provides` executable resolves on that PATH for kinds apt, binary
and toolchain. For kind apt, dpkg-query vouches only for the package, so each
executable's resolved file must also be owned by one of the entry's `owners`
(default: the entry itself); a shadowing or alternative binary is a MISMATCH.
The host verdict per entry is its worst runner's, naming that
runner. Read-only: it installs, writes and restarts nothing.

Per entry it prints one of:
  OK        verify exited 0 and its output carries the pinned version
  MISMATCH  verify exited 0 but the version differs (wanted vs got), or an apt
            executable resolves to a file another package (or none) owns
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
  ✔︎ ✅ 🎯 R3 every configured runner dir (holding `.runner`) the user owns is
    verified under its own `.path`, and the host verdict is the worst runner's.
    [if] one of several runners lacks a tool the others have [then ⛔️] OK.
    [if] the unsuffixed `actions-runner` dir is skipped [then ⛔️] it is verified.
    [if] one configured runner has neither a .env PATH= nor a readable .path
      [then ⛔️] any entry reports OK.
    [if] a runner's .env sets PATH= [then ⛔️] its .path is what gets verified.
    [if] a pinned tool reports the pin plus a `-nightly`/`-rc` suffix [then ⛔️] OK.
  ✔︎ ✅ 🎯 R4 an apt entry's executables are the ones its package installed.
    [if] the PATH-resolved executable belongs to another package [then ⛔️] OK.
    [if] it belongs to no package (a /usr/local or home-dir binary) [then ⛔️] OK.
    [if] an apt executable leaves no ownership record [then ⛔️] OK.
  ✔︎ ✅ R2 exit code follows the contract above.
    [if] one entry is UNKNOWN and the rest OK [then ⛔️] exit 0.
    [if] one entry is MISSING [then ⛔️] exit 0 or 2.
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import re
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass, field

from scripts.runner_toolset_scan import REPO_ROOT, load_manifest

CFG_TIMEOUT_PER_ENTRY_S = 60
CFG_SSH_TIMEOUT_S = 900
# A configured runner install is a dir holding `.runner`; a host may run many
# (agentbox: actions-runner plus -2..-15; nucbox: plus -2..-30).
CFG_RUNNER_DIR_GLOBS = (
    "/opt/actions-runner /opt/actions-runner-* /home/*/actions-runner /home/*/actions-runner-*"
)
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


def _probe_script(
    entries: list[dict], runner_dir: str | None, runner_dir_globs: str = CFG_RUNNER_DIR_GLOBS
) -> str:
    """One bash script that runs every verify once per distinct runner job PATH.

    Without `runner_dir` it verifies every configured runner dir the user owns:
    the job PATH is per runner install, so one runner's vouches for no other.
    A runner's job PATH is resolved as its Listener does: `.env`'s `PATH=` when
    set (the Listener loads `.env` and that wins), else `.path` (which runsvc.sh
    only exports as a fallback). Runners with the same effective PATH get
    identical verdicts and are probed once. `runner_dir_globs` (space-separated
    shell globs) is where runner dirs are looked for; `--runner-dir-globs`."""
    given = shlex.quote(runner_dir) if runner_dir else "''"
    lines = [
        "set -u",
        f"given={given}",
        "rds=()",
        'if [ -n "$given" ]; then rds=("$given"); else',
        f"  for d in {runner_dir_globs}; do",
        '    [ -d "$d" ] && [ -O "$d" ] && [ -f "$d/.runner" ] || continue',
        '    case " ${rds[*]:-} " in *" $d "*) ;; *) rds+=("$d") ;; esac',
        "  done",
        "fi",
        # The job PATH is a prerequisite: the invoking shell's PATH says nothing
        # about what a job resolves, so without it nothing is verified (UNKNOWN).
        'if [ "${#rds[@]}" -eq 0 ]; then',
        '  echo "no configured runner dir (with .runner) owned by this user under'
        f' {runner_dir_globs}; refusing to verify against the login PATH" >&2',
        "  exit 96",
        "fi",
        # The dpkg package owning the file an executable resolves to: symlinks and
        # alternatives followed; a merged-/usr path is recorded under /bin or /sbin.
        "owner_of() {",
        # `unmeasured` when the lookup itself cannot run: never a verdict about it.
        '  r="$(readlink -f "$1")" || { printf \'unmeasured %s\' "$1"; return; }',
        '  o=""',
        '  for f in "$r" "${r#/usr}"; do',
        '    out="$(dpkg-query -S "$f" 2>/dev/null)"; rc=$?',
        '    [ "$rc" -gt 1 ] && { printf \'unmeasured %s\' "$r"; return; }',
        "    while IFS= read -r line; do",
        '      case "$line" in "diversion by "*|"") ;; *) o="$line"; break ;; esac',
        '    done <<< "$out"',
        '    [ -n "$o" ] && break',
        "  done",
        '  o="${o%%: /*}"; o="${o// /}"',
        '  printf \'%s %s\' "${o:--}" "$r"',
        "}",
        # Same resolution as ci-hosts' runner-conformance.sh: .env PATH= wins.
        "job_path() {",
        '  p="$(grep -m1 \'^PATH=\' "$1/.env" 2>/dev/null | cut -d= -f2-)"',
        '  [ -n "$p" ] || p="$(head -n1 "$1/.path" 2>/dev/null)"',
        '  printf %s "$p"',
        "}",
        "paths=()",
        'for rd in "${rds[@]}"; do',
        '  path="$(job_path "$rd")"',
        '  if [ -z "$path" ]; then',
        '    echo "no job PATH for $rd: neither a PATH= in .env nor a readable, non-empty'
        ' .path; refusing to verify against the login PATH" >&2',
        "    exit 96",
        "  fi",
        '  paths+=("$path")',
        "done",
        "probed=()",
        "probed_paths=()",
        "i=0",
        'for rd in "${rds[@]}"; do',
        '  path="${paths[$i]}"; i=$((i + 1)); same=""',
        "  j=0",
        '  for p in "${probed_paths[@]:-}"; do',
        '    [ -n "$p" ] && [ "$p" = "$path" ] && same="${probed[$j]}" && break',
        "    j=$((j + 1))",
        "  done",
        f'  if [ -n "$same" ]; then echo "{RECORD} ALSO $rd $same"; continue; fi',
        '  probed+=("$rd")',
        '  probed_paths+=("$path")',
        '  export PATH="$path"',
        f'  echo "{RECORD} PATHSRC $rd"',
        "  cd ~ || exit 97",
    ]
    for entry in entries:
        lines += ["  " + line for line in _probe_lines(entry)]
    lines += ["done", f'echo "{RECORD} END"']
    return "\n".join(lines) + "\n"


# A verify may carry a committed helper as `{repo_b64:<repo path>}`: the probe
# runs from the runner user's home, where no checkout exists, so the file's
# base64 travels inside the command. A missing file raises: never a blank probe.
REPO_B64_RE = re.compile(r"\{repo_b64:([^}]+)\}")


def expand_verify(verify: str) -> str:
    """`verify` with every `{repo_b64:<path>}` replaced by that repo file's base64."""
    return REPO_B64_RE.sub(
        lambda m: base64.b64encode((REPO_ROOT / m.group(1)).read_bytes()).decode(), verify
    )


def _probe_lines(entry: dict) -> list[str]:
    name = entry["name"]
    verify = base64.b64encode(expand_verify(entry["verify"]).encode()).decode()
    run = (
        f"out=$(timeout {CFG_TIMEOUT_PER_ENTRY_S} bash -o pipefail -c "
        f'"$(echo {verify} | base64 -d)" 2>&1); rc=$?; '
        f'echo "{RECORD} V {name} $rc $(printf %s "$out" | head -c 2000 | base64 -w0)"'
    )
    on_path = entry.get("provides", []) if entry["kind"] in PATH_KINDS else []
    if entry["kind"] != "apt":
        return [run] + [
            f'command -v {shlex.quote(exe)} >/dev/null 2>&1 || echo "{RECORD} P {name} {exe}"'
            for exe in on_path
        ]
    return [run] + [
        f"if p=$(command -v {shlex.quote(exe)}); then"
        f' case "$p" in /*) echo "{RECORD} O {name} {exe} $(owner_of "$p")" ;;'
        f' *) echo "{RECORD} O {name} {exe} builtin $p" ;; esac;'
        f' else echo "{RECORD} P {name} {exe}"; fi'
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
    """One job PATH's records: verify results and provided executables off PATH."""

    verified: dict[str, tuple[int, str]] = field(default_factory=dict)
    off_path: dict[str, list[str]] = field(default_factory=dict)
    # entry -> exe -> (comma-separated owning packages or `-`/`builtin`, resolved path)
    owned: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)


@dataclass
class _Probe:
    """Records per runner dir whose job PATH was probed, in probe order."""

    by_runner: dict[str, _Records] = field(default_factory=dict)
    complete: bool = False


def _parse_records(stdout: str) -> _Probe:
    probe, current = _Probe(), ""
    for line in stdout.splitlines():
        parts = line.split(" ")
        if parts[0] != RECORD or len(parts) < 2:
            continue
        if parts[1] == "PATHSRC":
            current = " ".join(parts[2:])
            probe.by_runner.setdefault(current, _Records())
        elif parts[1] == "V":
            payload = parts[4] if len(parts) > 4 else ""
            output = base64.b64decode(payload).decode(errors="replace")
            probe.by_runner.setdefault(current, _Records()).verified[parts[2]] = (
                int(parts[3]),
                output,
            )
        elif parts[1] == "P":
            records = probe.by_runner.setdefault(current, _Records())
            records.off_path.setdefault(parts[2], []).append(parts[3])
        elif parts[1] == "O" and len(parts) >= 5:
            records = probe.by_runner.setdefault(current, _Records())
            records.owned.setdefault(parts[2], {})[parts[3]] = (parts[4], " ".join(parts[5:]))
        elif parts[1] == "END":
            probe.complete = True
    return probe


def _classify_one(entry: dict, records: _Records) -> tuple[str, str] | None:
    """(status, what was seen) for one entry under one job PATH; None if unreached."""
    name = entry["name"]
    if name not in records.verified:
        return None
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
    if entry["kind"] == "apt" and (foreign := _foreign_executables(entry, records)):
        return foreign
    if _version_matches(entry["version"], default_match(entry), output):
        return "OK", first
    return "MISMATCH", first


_OWNER_TEXT = {"-": "no package owns it", "builtin": "a shell builtin, no file"}


def _foreign_executables(entry: dict, records: _Records) -> tuple[str, str] | None:
    """An apt entry's executables must resolve to files one of its `owners` owns."""
    owners = entry.get("owners", [entry["name"]])
    seen = records.owned.get(entry["name"], {})
    unrecorded = [exe for exe in entry.get("provides", []) if exe not in seen]
    unmeasured = [exe for exe, (packages, _) in seen.items() if packages == "unmeasured"]
    if unrecorded or unmeasured:
        return "UNKNOWN", f"package ownership not measured for {', '.join(unrecorded + unmeasured)}"
    foreign = [
        f"{exe} runs {path} ({_OWNER_TEXT.get(packages, 'owned by ' + packages)})"
        for exe, (packages, path) in seen.items()
        if not any(
            fnmatch.fnmatchcase(package.split(":")[0], pattern)
            for package in packages.split(",")
            for pattern in owners
        )
    ]
    return ("MISMATCH", f"{'; '.join(foreign)}, not {'/'.join(owners)}") if foreign else None


# The verdict for a host is its worst runner's: one runner job PATH lacking a
# tool fails every job scheduled onto that runner.
SEVERITY = {"OK": 0, "INFO": 1, "UNKNOWN": 2, "MISMATCH": 3, "MISSING": 4}


def _classify_host(entry: dict, probe: _Probe, unreached: str) -> tuple[str, str]:
    """The worst verdict across every probed runner job PATH, naming the runner."""
    if not probe.complete or not probe.by_runner:
        return "UNKNOWN", unreached
    verdicts = {
        runner: _classify_one(entry, records) for runner, records in probe.by_runner.items()
    }
    reached = {runner: v for runner, v in verdicts.items() if v is not None}
    if len(reached) < len(verdicts):
        return "UNKNOWN", unreached
    runner = max(reached, key=lambda r: SEVERITY[reached[r][0]])
    status, got = reached[runner]
    if len(reached) > 1 and status not in {"OK", "INFO"}:
        return status, f"{runner}: {got}"
    return status, got


def classify(entries: list[dict], rc: int, stdout: str, stderr: str) -> list[Result]:
    """Turn the probe's records into one Result per entry."""
    probe = _parse_records(stdout)
    unreached = stderr.strip().splitlines()[-1] if stderr.strip() else f"probe exit {rc}, no record"
    results = []
    for entry in entries:
        status, got = _classify_host(entry, probe, unreached)
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
        help="verify only this runner dir's .path (default: every configured runner dir"
        " the user owns)",
    )
    parser.add_argument(
        "--runner-dir-globs",
        default=CFG_RUNNER_DIR_GLOBS,
        help="space-separated shell globs where runner dirs are looked for"
        f" (default: {CFG_RUNNER_DIR_GLOBS})",
    )
    parser.add_argument("--only", help="comma-separated entry names")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        entries = _select_entries(args.only)
    except (OSError, TypeError, ValueError) as exc:
        print(f"[ERROR] manifest: {exc}", file=sys.stderr)
        return 2
    script = _probe_script(entries, args.runner_dir, args.runner_dir_globs)
    rc, stdout, stderr = _run_probe(script, None if args.local else args.host, args.user)
    results = classify(entries, rc, stdout, stderr)
    probed = [
        ln.split(" ", 2)[2] for ln in stdout.splitlines() if ln.startswith(f"{RECORD} PATHSRC")
    ]
    also = sum(ln.startswith(f"{RECORD} ALSO ") for ln in stdout.splitlines())
    header = {
        "host": args.host or "local",
        "user": args.user,
        "path_from": ",".join(probed) or "UNKNOWN",
        "runner_dirs": str(len(probed) + also),
    }
    _print_report(results, header, args.json)
    return exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
