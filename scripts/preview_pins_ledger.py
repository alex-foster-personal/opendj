"""Check that every Preview comment pin is accounted for in requirements and tests.

Usage:
  uv run --no-sync python -m scripts.preview_pins_ledger
  uv run --no-sync python -m scripts.preview_pins_ledger --feedback-dir <data-dir>/feedback

The ledger is ``docs/controller/preview-pins-ledger.json``. With no
``--feedback-dir`` the pin set comes from the committed snapshots it names; with
one, the Preview-build pins in that live store must also be in the ledger (read
only, never written). Exit 0 prints the pin count; exit 1 prints every problem.

Requirements (acceptance tests in tests/scripts/test_preview_pins_ledger.py):
  ✔︎ ✅ 🎯 Ledger and snapshots agree both ways on the Preview pin set.
    [if] a snapshot pin is missing from the ledger [then ⛔️]
    [if] the ledger names a pin no snapshot holds [then ⛔️]
    [if] a live store holds a Preview-build pin the ledger lacks [then ⛔️]
  ✔︎ ✅ 🎯 Every cited requirement row exists and names the pin.
    [if] a cited id has no ``- [ ] **ID**`` / ``- [x] **ID**`` row [then ⛔️]
    [if] the row and its sub-bullets never mention the pin id [then ⛔️]
  ✔︎ ✅ 🎯 Built work has regression tests; unbuilt work stays open.
    [if] a shipped [x] row cited by a pin has no test file naming it [then ⛔️]
    [if] a shipped or partial pin has no cited row with a test [then ⛔️]
    [if] a partial, deferred, held or open pin cites no [ ] row [then ⛔️]
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEDGER = PROJECT_ROOT / "docs" / "controller" / "preview-pins-ledger.json"
REQUIREMENTS = PROJECT_ROOT / ".planning" / "REQUIREMENTS.md"
TEST_ROOTS = ("tests", "apps")
TEST_NAME = re.compile(r"(^test_.*\.py$|_test\.py$|\.test\.(mjs|ts|js)$|\.spec\.ts$)")
SKIP_DIRS = {"node_modules", "fixtures", ".svelte-kit", "build", "dist", "__pycache__", ".venv"}
DISPOSITIONS = {"shipped", "partial", "deferred", "held", "open"}
BUILT = {"shipped", "partial"}
ROW = re.compile(r"^- \[( |x)\] \*\*([A-Za-z0-9-]+)\*\*")
REQ_TOKEN = re.compile(
    r"(?<![A-Za-z0-9-])[A-Z][A-Z0-9]*(?:-[A-Z][A-Z0-9]*)*-\d+[a-z]?(?![A-Za-z0-9])"
)


@dataclass(frozen=True)
class ReqRow:
    shipped: bool
    block: str


# ----------------------------------------------------------------- readers


def read_requirement_rows(text: str) -> dict[str, ReqRow]:
    """Map each checkbox requirement id to its row line plus indented sub-lines."""
    rows: dict[str, ReqRow] = {}
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = ROW.match(line)
        if match is None:
            continue
        block = [line]
        for follow in lines[index + 1 :]:
            if not follow.startswith((" ", "\t")):
                break
            block.append(follow)
        if match.group(2) in rows:
            raise ValueError(f"requirement {match.group(2)} has two checkbox rows")
        rows[match.group(2)] = ReqRow(shipped=match.group(1) == "x", block="\n".join(block))
    return rows


def read_snapshot_pin_ids(root: Path, snapshot_paths: list[str], builds: set[str]) -> set[str]:
    """Pin ids (and general-note keys) in the committed snapshots for Preview builds."""
    ids: set[str] = set()
    for rel in snapshot_paths:
        data = json.loads((root / rel).read_text(encoding="utf-8"))
        if "stores" in data:
            for store in data["stores"]:
                ids |= _preview_ids(store["pins"], builds)
                if "general_note" in store:
                    ids.add(f"general-note-{store['general_note']['build']['git_sha']}")
        elif "pins" in data:
            ids |= _preview_ids(data["pins"], builds)
        elif "comments" in data:
            ids |= _preview_ids(data["comments"], builds)
        else:
            raise ValueError(f"{rel} holds no pins, comments or stores")
    return ids


def read_live_pin_ids(feedback_dir: Path, builds: set[str]) -> set[str]:
    """Preview-build pin ids in a live feedback store (read only)."""
    comments = feedback_dir / "comments.json"
    if not comments.is_file():
        raise ValueError(f"no comments.json in {feedback_dir}")
    ids = _preview_ids(json.loads(comments.read_text(encoding="utf-8"))["comments"], builds)
    note = feedback_dir / "general-note.json"
    if note.is_file():
        sha = (json.loads(note.read_text(encoding="utf-8")).get("build") or {}).get("git_sha")
        if sha in builds:
            ids.add(f"general-note-{sha}")
    return ids


def index_test_files(root: Path) -> dict[Path, str]:
    """Every test source under the test roots, excluding fixtures and node_modules."""
    files: dict[Path, str] = {}
    for top in TEST_ROOTS:
        for dirpath, dirnames, filenames in os.walk(root / top):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                if TEST_NAME.search(name):
                    path = Path(dirpath) / name
                    files[path] = path.read_text(encoding="utf-8", errors="replace")
    return files


def _preview_ids(pins: list[dict], builds: set[str]) -> set[str]:
    return {p["id"] for p in pins if (p.get("build") or {}).get("git_sha", "")[:8] in builds}


# ----------------------------------------------------------------- checks


def find_problems(
    ledger: dict, rows: dict[str, ReqRow], tests: dict[Path, str], snapshot_ids: set[str]
) -> list[str]:
    """Every way the ledger fails to account for a Preview pin; empty means complete."""
    problems: list[str] = []
    pins = {p["id"]: p for p in ledger["pins"]}
    problems += [
        f"pin {i}: in a snapshot, missing from the ledger"
        for i in sorted(snapshot_ids - pins.keys())
    ]
    problems += [
        f"pin {i}: in the ledger, in no snapshot" for i in sorted(pins.keys() - snapshot_ids)
    ]
    tested = {rid for text in tests.values() for rid in REQ_TOKEN.findall(text)}
    for pin_id, pin in sorted(pins.items()):
        disposition = pin["disposition"]
        if disposition not in DISPOSITIONS:
            problems.append(f"pin {pin_id}: unknown disposition {disposition!r}")
        if not pin["requirements"]:
            problems.append(f"pin {pin_id}: cites no requirement")
        for rid in pin["requirements"]:
            row = rows.get(rid)
            if row is None:
                problems.append(f"pin {pin_id}: requirement {rid} has no checkbox row")
            elif pin_id not in row.block:
                problems.append(f"pin {pin_id}: requirement {rid} row does not name the pin")
            elif row.shipped and rid not in tested:
                problems.append(f"pin {pin_id}: shipped requirement {rid} has no test naming it")
        cited = [rows[r] for r in pin["requirements"] if r in rows]
        if disposition in BUILT and not any(r in tested for r in pin["requirements"]):
            problems.append(f"pin {pin_id}: {disposition} but no cited requirement has a test")
        if disposition != "shipped" and cited and all(r.shipped for r in cited):
            problems.append(f"pin {pin_id}: {disposition} but every cited row is [x]")
    return problems


def check(root: Path = PROJECT_ROOT, feedback_dirs: list[Path] | None = None) -> list[str]:
    ledger = json.loads((root / LEDGER.relative_to(PROJECT_ROOT)).read_text(encoding="utf-8"))
    builds = set(ledger["preview_builds"])
    rows = read_requirement_rows(
        (root / REQUIREMENTS.relative_to(PROJECT_ROOT)).read_text(encoding="utf-8")
    )
    snapshot_ids = read_snapshot_pin_ids(root, ledger["snapshots"], builds)
    problems = find_problems(ledger, rows, index_test_files(root), snapshot_ids)
    ledger_ids = {p["id"] for p in ledger["pins"]}
    for feedback_dir in feedback_dirs or []:
        live = read_live_pin_ids(feedback_dir, builds)
        problems += [
            f"pin {i}: in live store {feedback_dir}, missing from the ledger"
            for i in sorted(live - ledger_ids)
        ]
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--feedback-dir", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    problems = check(PROJECT_ROOT, args.feedback_dir)
    if problems:
        print("\n".join(f"[ERROR] {p}" for p in problems))
        return 1
    count = len(json.loads(LEDGER.read_text(encoding="utf-8"))["pins"])
    print(f"[OK] {count} Preview pins accounted for in requirements and tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
