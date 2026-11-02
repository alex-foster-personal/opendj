"""Build the oss audit kernel and export the backend env for the CI job that runs the suite.

Both ci.yml and runner-canary.yml call this one entry point (AGENTS.md portable-by-default:
CI machinery is Python, not shell). It runs the cargo build, checks the binary exists, and
appends the two backend variables to GITHUB_ENV. ODJ_OSS_AUDIT_BACKEND=auto with the binary
present routes the suite's audits through the kernel; ODJ_REQUIRE_OSS_AUDIT_NATIVE=1 makes a
missing kernel a failure instead of a quiet Python fallback.

The build self-heals a corrupted persistent target/ dir the way scripts/ci_cargo_selfheal.sh
does for the other cargo steps: on the torn-target signature only, clean this profile and retry
once. The signature is the same and is checked by the same rule (E0786, or E0463 naming a crate
Cargo.lock says should resolve). A genuine compile error fails on the first run.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = "scripts/native/oss_tip_audit/Cargo.toml"
BINARY_NAME = "oss-tip-audit-native"
PROFILE = "release"

_E0786 = re.compile(r"^error\[E0786\]: found invalid metadata files", re.MULTILINE)
_E0463 = re.compile(r"^error\[E0463\]: can't find crate for `([^`]+)`", re.MULTILINE)


def is_corrupted_target(log: str, lockfile: Path) -> bool:
    """True only for the two torn-target signatures; any other failure is a real error.

    What could satisfy this without satisfying its intent? A compile error that happens to
    print a matching line from somewhere else. So both signatures are anchored on cargo's own
    `error[Exxxx]:` line, and E0463 must name a crate Cargo.lock says should resolve.
    """
    if _E0786.search(log):
        return True
    crate = _E0463.search(log)
    if crate is None:
        return False
    lock = lockfile.read_text(encoding="utf-8") if lockfile.is_file() else ""
    # rustc names a crate with underscores where Cargo.lock keeps the package's hyphens.
    wanted = crate.group(1).replace("-", "_")
    return any(name.replace("-", "_") == wanted for name in re.findall(r'^name = "([^"]+)"$', lock, re.MULTILINE))


def executable_from_cargo(report: str) -> Path | None:
    """The kernel's executable as cargo reports it in its JSON report, or None.

    Cargo knows where it wrote the file: a configured build target adds a triple directory and a
    configured target dir moves the whole tree. Reading cargo's own report keeps every caller on
    the file that was built, and the Python lookup reads the path this helper exports.

    The report is JSON only, so every line is parsed before any is read: a line that is not JSON
    raises ValueError, and one that is not an object raises TypeError. Skipping a stray line beside
    a valid artifact record would accept a report that is not to be trusted.
    """
    messages = []
    for line in report.splitlines():
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"cargo report line is not JSON: {line[:200]!r}") from exc
        if not isinstance(message, dict):
            raise TypeError(f"cargo report line is not a JSON object: {line[:200]!r}")
        messages.append(message)
    for message in messages:
        if message.get("reason") != "compiler-artifact":
            continue
        target = message.get("target")
        executable = message.get("executable")
        if isinstance(target, dict) and target.get("name") == BINARY_NAME and isinstance(executable, str):
            return Path(executable)
    return None


def _cargo(cargo: str, *args: str) -> tuple[int, str, str]:
    """One cargo invocation: its exit code, its JSON report, and its diagnostics.

    The diagnostics are echoed to the log. The report is parsed, not shown.
    """
    run = subprocess.run([cargo, *args], cwd=ROOT, capture_output=True, check=False)
    report = run.stdout.decode("utf-8", errors="replace")
    diagnostics = run.stderr.decode("utf-8", errors="replace")
    print(diagnostics, end="")
    return run.returncode, report, diagnostics


def build(cargo: str) -> tuple[int, str]:
    """The release build, retried once after cleaning this profile when the target is torn.

    Returns the exit status and cargo's JSON report, which names the kernel's path.
    """
    build_args = (
        "build",
        "--release",
        "--locked",
        "--message-format=json-render-diagnostics",
        "--manifest-path",
        MANIFEST,
    )
    status, report, diagnostics = _cargo(cargo, *build_args)
    if status == 0 or not is_corrupted_target(diagnostics, (ROOT / MANIFEST).with_name("Cargo.lock")):
        return status, report
    print(
        f"::warning::corrupted cargo target/ dir (manifest {MANIFEST}): "
        f"cleaning profile '{PROFILE}' and retrying the build once"
    )
    _cargo(cargo, "clean", "--manifest-path", MANIFEST, "--profile", PROFILE)
    status, report, _diagnostics = _cargo(cargo, *build_args)
    return status, report


def main() -> int:
    # cargo is launched by resolved path; a missing one fails here with a named reason.
    cargo = shutil.which("cargo")
    if cargo is None:
        print("::error title=oss audit kernel toolchain missing::cannot find cargo on PATH")
        return 2
    started = time.monotonic()
    status, report = build(cargo)
    if status != 0:
        print(f"::error title=oss audit kernel build failed::cargo build exited {status}")
        return status
    try:
        binary = executable_from_cargo(report)
    except (ValueError, TypeError) as exc:
        print(f"::error title=oss audit kernel report malformed::{exc}")
        return 1
    if binary is None or not binary.is_file() or not os.access(binary, os.X_OK):
        print(f"::error title=oss audit kernel missing::cargo build exited 0 and reported no executable {BINARY_NAME}")
        return 1
    print(f"[oss-audit-kernel] ready in {time.monotonic() - started:.0f}s: {binary}")
    github_env = os.environ.get("GITHUB_ENV")
    if github_env:
        with open(github_env, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                f"ODJ_OSS_AUDIT_BACKEND=auto\nODJ_REQUIRE_OSS_AUDIT_NATIVE=1\nODJ_OSS_AUDIT_NATIVE_BIN={binary}\n"
            )
    else:
        print(
            "GITHUB_ENV is not set (local run): export ODJ_OSS_AUDIT_BACKEND=auto "
            f"ODJ_REQUIRE_OSS_AUDIT_NATIVE=1 ODJ_OSS_AUDIT_NATIVE_BIN={binary} to use the kernel"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
