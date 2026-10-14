"""How many test modules the PR-head selector picks for each webui server module (BigRfc-1).

For every Python module under `apps/webui/server`, this asks the DEVOPS-20 selector
(`scripts/ci_test_selection_rules.select_tests`) what a pull request changing only that
file would select, with the shard lane's own `--ignore` list, and prints the median and
mean. It is the measurement behind (private path):
run it on two trees (or with two rule files) and compare, rather than trusting a number
copied from the ADR.

    python -m scripts.selection_reach_census --out /tmp/after.json
    python -m scripts.selection_reach_census --rules /tmp/other_rules.py --out /tmp/x.json

`--rules` loads a different `ci_test_selection_rules.py` (for example an open PR's
version, read with `git show "${SHA}:scripts/ci_test_selection_rules.py"`) against this
tree. A module the selector answers with FullSuite is counted separately, never as a
number, so a census that could not narrow says so instead of reporting a small median.

Read-only: it parses the tree and writes only `--out`.
"""

from __future__ import annotations

import argparse
import functools
import importlib.util
import json
import statistics
import sys
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parents[1]

#: The shard lane's `--ignore` list, as `.github/workflows/ci.yml` passes it to
#: `scripts.ci_test_selection select`.
LANE_IGNORES: tuple[str, ...] = (
    "tests/analysis",
    "tests/vocals",
    "tests/sync/usb",
    "tests/sets",
    "tests/quality/test_frontend_typing_gate.py",
    "tests/quality/test_frontend_typing_directives.py",
    "tests/quality/test_mypy_cache.py",
    "tests/perf/test_library_playlist_switch_bench.py",
)

SUBJECT = "apps/webui/server"


def _load_rules(path: Path | None) -> ModuleType:
    if path is None:
        import scripts.ci_test_selection_rules as rules

        return rules
    name = "scripts.ci_test_selection_rules"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load selector rules from {path}")
    rules = importlib.util.module_from_spec(spec)
    sys.modules[name] = rules
    spec.loader.exec_module(rules)
    return rules


def census(root: Path, rules: ModuleType) -> dict[str, dict[str, object]]:
    """Per server module: the selected count and how many are tests/webui, or FullSuite."""
    load = rules.Graph.load
    rules.Graph.load = functools.lru_cache(maxsize=None)(load)  # one graph build, not 229
    paths = sorted(str(p.relative_to(root)) for p in (root / SUBJECT).rglob("*.py") if "__pycache__" not in p.parts)
    out: dict[str, dict[str, object]] = {}
    for path in paths:
        try:
            selection = rules.select_tests([path], list(LANE_IGNORES), root)
        except rules.FullSuite as exc:
            out[path] = {"full_suite": str(exc)}
            continue
        out[path] = {
            "selected": len(selection.modules),
            "webui": sum(m.startswith("tests/webui/") for m in selection.modules),
        }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=REPO)
    parser.add_argument("--rules", type=Path, default=None, help="another ci_test_selection_rules.py")
    parser.add_argument("--out", type=Path, required=True, help="per-module JSON")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(args.root.resolve()))
    result = census(args.root.resolve(), _load_rules(args.rules))
    args.out.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    counted = [v["selected"] for v in result.values() if "selected" in v]
    full = len(result) - len(counted)
    if not counted:
        print(f"UNKNOWN: no module narrowed ({full} FullSuite of {len(result)})")
        return 2
    webui = [v["webui"] for v in result.values() if "webui" in v]
    print(
        f"{len(result)} modules, {full} FullSuite; selected median {statistics.median(counted)}, "
        f"mean {statistics.mean(counted):.1f}; tests/webui median {statistics.median(webui)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
