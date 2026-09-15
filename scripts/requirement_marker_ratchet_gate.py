"""IDD merge gate: run requirement-marker ratchets before merge.

Delegates to pytest; never reimplements ratchet logic.

  - [if] a lane PR adds a marked test with no intent line [then] refuse and
    name the scope, [else stop].
  - [if] the check cannot run (missing venv, collection error) [then] report
    UNKNOWN and refuse, [else stop].

    python -m scripts.requirement_marker_ratchet_gate
    python -m scripts.requirement_marker_ratchet_gate --repo-root /path/to/worktree
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PREFIX = "[requirement-marker-ratchet-gate]"

PYTEST_TARGETS: tuple[str, ...] = (
    "tests/test_requirement_markers.py",
    "tests/test_requirement_intent_ratchet.py",
    "tests/quality/test_gate_scope.py",
)

_VALID_SCOPE = re.compile(r"^tests/[A-Za-z0-9_./:-]+(?:::[A-Za-z0-9_]+)?$")
_SCOPE_LINE = re.compile(r"^\s*(?:E\s+)?(tests/[^\s:]+\.py(?:::[^\s]+)?)\s*$")
_UNREGISTERED = re.compile(
    r"(?:AssertionError:\s*)?unregistered gate suites:\s*((?:tests/[^,\s]+(?:,\s*)?)+)\.\s+Add each"
)
_STALE_KNOWN = re.compile(
    r"(?:AssertionError:\s*)?KNOWN_UNSCOPED is out of date:\s*\[([^\]]+)\]"
)
_MISSING_SCOPE = re.compile(
    r"(?:AssertionError:\s*)?pytest_scope names ([^,]+), which does not exist"
)
_INTENT_BLOCK = re.compile(
    r"Marked pytest scopes need[^:]*:\s*\n((?:\s+(?:E\s+)?tests/[^\n]+\n?)+)",
    re.MULTILINE,
)
def _scopes_from_intent_blocks(output: str) -> list[str]:
    """Scopes listed under a 'Marked pytest scopes need ...' assertion."""
    found: list[str] = [
        hit.group(1)
        for match in _INTENT_BLOCK.finditer(output)
        for hit in map(_SCOPE_LINE.match, match.group(1).splitlines())
        if hit
    ]
    if "Marked pytest scopes need" not in output:
        return found
    collecting = False
    for line in output.splitlines():
        if "Marked pytest scopes need" in line:
            collecting = True
            continue
        if not collecting:
            continue
        hit = _SCOPE_LINE.match(line)
        if hit:
            found.append(hit.group(1))
        elif line.strip() and found:
            collecting = False
    return found


def _scopes_from_assertions(output: str) -> list[str]:
    """Scopes named by the unregistered, stale-known and missing-scope assertions."""
    found: list[str] = []
    for match in _UNREGISTERED.finditer(output):
        found.extend(re.split(r",\s*", match.group(1).strip()))
    for match in _STALE_KNOWN.finditer(output):
        found.extend(re.findall(r"'([^']+)'", match.group(1)))
    for match in _MISSING_SCOPE.finditer(output):
        found.append(match.group(1))
    return found


def extract_scopes(output: str) -> list[str]:
    """Return deduplicated scope/suite paths parsed from pytest failure output."""
    scopes: list[str] = []
    seen: set[str] = set()
    for raw in _scopes_from_intent_blocks(output) + _scopes_from_assertions(output):
        cleaned = raw.strip().strip("'\"")
        if cleaned and _VALID_SCOPE.match(cleaned) and cleaned not in seen:
            seen.add(cleaned)
            scopes.append(cleaned)
    return scopes


def _is_collection_failure(returncode: int, output: str) -> bool:
    if "ERROR collecting" in output:
        return True
    if returncode in {2, 4, 5}:
        return True
    return returncode != 0 and "no tests ran" in output.lower()


def run_pytest(repo_root: Path) -> tuple[int, str]:
    """Run the pinned pytest trio from ``repo_root`` and return (rc, combined output)."""
    if shutil.which("uv") is None:
        raise FileNotFoundError("uv not found on PATH")
    cmd = ["uv", "run", "--no-sync", "pytest", "-q", *PYTEST_TARGETS]
    proc = subprocess.run(
        cmd,
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def main(argv: list[str] | None = None, *, runner=run_pytest) -> int:
    parser = argparse.ArgumentParser(prog="requirement_marker_ratchet_gate")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="Worktree root containing pyproject.toml (default: this checkout)",
    )
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()

    if not (repo_root / "pyproject.toml").is_file():
        print(f"{PREFIX} UNKNOWN: no pyproject.toml in {repo_root}", file=sys.stderr)
        return 2

    try:
        returncode, output = runner(repo_root)
    except FileNotFoundError as exc:
        print(f"{PREFIX} UNKNOWN: {exc}", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        print(f"{PREFIX} UNKNOWN: pytest timed out after 180s", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"{PREFIX} UNKNOWN: {exc}", file=sys.stderr)
        return 2

    if returncode == 0:
        print(f"{PREFIX} OK")
        return 0

    if _is_collection_failure(returncode, output):
        print(f"{PREFIX} UNKNOWN: pytest collection failed (rc={returncode})", file=sys.stderr)
        if output.strip():
            print(output[-4000:], file=sys.stderr)
        return 2

    scopes = extract_scopes(output)
    scope_field = ", ".join(scopes) if scopes else "unknown"
    print(f"{PREFIX} REFUSED scopes={scope_field}")
    if output.strip():
        print(output[-8000:], file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
