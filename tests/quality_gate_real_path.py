"""Real-path replacement for issue #3246 main-report-only and merge-base tests (issue #3335).

The monkeypatched block in (private path) (review PR #3291) never exercised
the real gate verdict. These tests copy the gate modules into a disposable git repo,
run `python -m scripts.quality_gate --only latency`, and assert exit codes and output.

The body lives here to keep (private path) under the file-size gate; it is
imported there so both `pytest (private path)` and
`pytest tests/quality_gate_real_path.py` collect the same cases.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from scripts import quality_gate as qg
from tests.quality.format_proof_repo import IDENTITY
from tests.scripts.sparse_worktree_fixtures import scrubbed_git_env

# A new top-level import in quality_gate.py must add its module here or the fixture gate crashes loud.
_GATE_MODULES: tuple[str, ...] = (
    "__init__.py",
    "quality_gate.py",
    "quality_latency.py",
    "shell_construct_lint.py",
    "shell_construct_lint_status.py",
    "ci_path_resolve.py",
    "sparse_worktree.py",
)

_BASELINE = json.dumps(
    {"metrics": {"latency.input_to_applied_ms": 30}, "slack": {}},
    indent=2,
)

_WIRED_SOURCES: dict[str, str] = {
    "apps/webui/frontend/src/lib/player/eq-apply.ts": (
        "export const EQ_APPLY_KIND = 'eq-apply';\n"
        "linearRampToValueAtTime; cancelScheduledValues;\n"
    ),
    "apps/webui/frontend/src/lib/player/mixer-apply.ts": (
        "export const MIXER = 'mixer-apply';\n"
    ),
    "apps/webui/frontend/src/lib/rb/audio-engine.svelte.ts": (
        "setEq(deck, band, value, pressT0Ms) { this._ramp(); }\n"
        "setFilter(deck, value, pressT0Ms) {}\n"
        "setFader(deck, value, pressT0Ms) {}\n"
        "setCrossfader(value, pressT0Ms) {}\n"
        "setStemMute(deck, stem, muted, pressT0Ms) {}\n"
        "setStemSolo(deck, stem, solo, pressT0Ms) {}\n"
    ),
    "apps/webui/frontend/src/lib/rb/performance-ipc.svelte.ts": (
        "engine.setEq(command.deck, command.band, command.value, pressT0Ms)\n"
        "engine.setFilter(command.deck, command.value, pressT0Ms)\n"
        "engine.setFader(command.deck, command.value, pressT0Ms)\n"
        "engine.setCrossfader(command.value, pressT0Ms)\n"
        "engine.setStemMute(command.deck, command.stem, command.muted, pressT0Ms)\n"
        "engine.setStemSolo(command.deck, command.stem, command.solo, pressT0Ms)\n"
    ),
    "apps/webui/frontend/src/lib/rb/perf-event-buckets.ts": (
        "'eq-apply' 'mixer-apply' 'filter-apply'\n"
    ),
}


def _env() -> dict[str, str]:
    env = scrubbed_git_env()
    env.pop("PYTHONPATH", None)
    return env


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
        env=_env(),
    )
    return proc.stdout.strip()


def _copy_gate_modules(root: Path, *, broken_latency: bool = False) -> None:
    scripts_dest = root / "scripts"
    scripts_dest.mkdir(parents=True, exist_ok=True)
    src_dir = Path(qg.__file__).parent
    for name in _GATE_MODULES:
        if name == "quality_latency.py" and broken_latency:
            (scripts_dest / name).write_text("def (:\n", encoding="utf-8")
        else:
            shutil.copy2(src_dir / name, scripts_dest / name)


def _write_tree(root: Path) -> None:
    (root / "ops" / "quality").mkdir(parents=True, exist_ok=True)
    (root / "ops" / "quality" / "baseline.json").write_text(_BASELINE, encoding="utf-8")
    for rel, content in _WIRED_SOURCES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _commit_all(root: Path, subject: str) -> str:
    _git(root, "add", "-A")
    _git(root, *IDENTITY, "commit", "-q", "-m", subject)
    return _git(root, "rev-parse", "HEAD")


def _build_fixture(tmp_path: Path, *, broken_latency: bool = False) -> Path:
    """Return a disposable repo root with gate modules and wired latency sources at HEAD.

    The fixture MUST be a git repo with at least one commit, or _hotspots returns UNKNOWN (exit 2).
    """
    root = tmp_path / "repo"
    root.mkdir()
    _copy_gate_modules(root, broken_latency=broken_latency)
    _write_tree(root)
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=root,
        check=True,
        env=_env(),
    )
    _commit_all(root, "wired latency fixture")
    return root


def _set_origin_main(root: Path, rev: str) -> None:
    _git(root, "update-ref", "refs/remotes/origin/main", rev)


def _unwire(root: Path) -> None:
    (root / "apps/webui/frontend/src/lib/player/eq-apply.ts").write_text(
        "broken\n",
        encoding="utf-8",
    )


def _restore_good_latency(root: Path) -> None:
    src = Path(qg.__file__).parent / "quality_latency.py"
    shutil.copy2(src, root / "scripts" / "quality_latency.py")


def _gate(root: Path, *extra: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.quality_gate", "--only", "latency", *extra],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=_env(),
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_main_report_only_prints_regression_but_exits_zero(tmp_path: Path) -> None:
    """On main push/schedule, plain-ratchet growth is visible but non-blocking."""
    root = _build_fixture(tmp_path)
    _unwire(root)
    _commit_all(root, "unwire for regression")
    _set_origin_main(root, _git(root, "rev-parse", "HEAD"))

    code, out = _gate(root, "--main-report-only")
    assert code == 0, (
        "if --main-report-only is set on a push-to-main run then the gate must "
        f"exit 0, but it exited {code}\n{out}"
    )
    assert "REGRESSION" in out, (
        f"the regression line must still print so the trend is visible\n{out}"
    )
    assert "TREND" in out, (
        f"the non-blocking trend label must print on main-report-only runs\n{out}"
    )

    code_plain, out_plain = _gate(root)
    assert code_plain == 1, (
        "without --main-report-only the same fixture must still fail the PR gate "
        f"so the flag is what made it pass, but it exited {code_plain}\n{out_plain}"
    )


def test_pr_mode_worse_than_merge_base_still_fails_without_main_report_only(
    tmp_path: Path,
) -> None:
    """PR runs without the flag still gate a genuine new violation."""
    root = _build_fixture(tmp_path)
    _set_origin_main(root, _git(root, "rev-parse", "HEAD"))
    _unwire(root)
    _commit_all(root, "break wiring")

    code, out = _gate(root)
    assert code == 1, (
        "if this run is above main's already-over value then the gate must "
        f"fail without --main-report-only, but it exited {code}\n{out}"
    )
    assert "FAIL: 1 metric(s) got worse." in out, f"missing FAIL line\n{out}"
    assert "this change is worse than main" in out, f"missing worse-than-main line\n{out}"


def test_pr_mode_inherited_still_passes_without_main_report_only(tmp_path: Path) -> None:
    """PR-mode INHERITED is unchanged: trunk debt is not blamed on this change."""
    root = _build_fixture(tmp_path)
    _unwire(root)
    broken_head = _commit_all(root, "trunk already unwired")
    _set_origin_main(root, broken_head)
    (root / "x.txt").write_text("unrelated\n", encoding="utf-8")
    _commit_all(root, "unrelated file only")

    code, out = _gate(root)
    assert code == 0, (
        "if main is already at this run's value then PR-mode must pass "
        f"without --main-report-only, but it exited {code}\n{out}"
    )
    assert "INHERITED" in out, f"missing INHERITED\n{out}"
    assert "ALSO at 999" in out, f"missing ALSO at 999\n{out}"
    assert "FAIL:" not in out, f"INHERITED must not print FAIL\n{out}"


def test_pr_mode_unmeasurable_merge_base_never_passes_silently(tmp_path: Path) -> None:
    """[if] merge-base cannot be measured [then] UNKNOWN exit 2, [else stop]."""
    root = _build_fixture(tmp_path, broken_latency=True)
    _set_origin_main(root, _git(root, "rev-parse", "HEAD"))
    _restore_good_latency(root)
    _unwire(root)
    _commit_all(root, "good latency on branch, broken on main")

    code, out = _gate(root)
    assert code == 2, (
        "if the merge-base main cannot be measured then the gate must exit 2 "
        f"(UNKNOWN), but it exited {code}\n{out}"
    )
    assert "UNKNOWN: base_compare:" in out, f"missing UNKNOWN base_compare\n{out}"
    assert "cannot re-measure latency on merge-base main" in out, (
        f"the run must name why it could not classify the metric\n{out}"
    )
    assert "REGRESSION" not in out, f"must not fake REGRESSION on unmeasurable base\n{out}"


def test_undecidable_base_compare_writes_unknown_to_json_and_report(tmp_path: Path) -> None:
    """[if] base compare is undecidable [then] artifacts show UNKNOWN, [else stop]."""
    root = _build_fixture(tmp_path)
    _unwire(root)
    _commit_all(root, "unwired head, no origin/main")

    report = root / "ops" / "quality" / "report.md"
    metrics = root / "ops" / "quality" / "metrics.json"
    code, out = _gate(
        root,
        "--report",
        str(report),
        "--json",
        str(metrics),
    )

    assert code == 2, f"undecidable base must exit 2, got {code}\n{out}"
    assert report.is_file() and metrics.is_file(), "report and metrics must be written"
    report_text = report.read_text(encoding="utf-8")
    metrics_data = json.loads(metrics.read_text(encoding="utf-8"))
    assert "base_compare" in report_text and "UNKNOWN" in report_text, (
        f"report must show base_compare UNKNOWN\n{report_text}"
    )
    base_cmp = metrics_data["measurements"]["base_compare"]
    assert base_cmp["status"] == "UNKNOWN"
    assert "git merge-base HEAD origin/main failed" in base_cmp["error"]
    assert "[quality] UNKNOWN" in out, f"stdout must flag UNKNOWN\n{out}"


def test_real_gate_fixture_mutation_flips_the_verdict(tmp_path: Path) -> None:
    """[if] one wiring file in the fixture is broken [then] the real gate flips PASS to FAIL, [else stop]."""
    root = _build_fixture(tmp_path)
    _set_origin_main(root, _git(root, "rev-parse", "HEAD"))

    code_pass, out_pass = _gate(root)
    assert code_pass == 0, f"wired fixture must pass, exited {code_pass}\n{out_pass}"
    assert "PASS: nothing got worse." in out_pass, f"missing PASS line\n{out_pass}"

    _unwire(root)
    _commit_all(root, "break eq-apply wiring")
    code_fail, out_fail = _gate(root)
    assert code_fail == 1, f"unwired fixture must fail, exited {code_fail}\n{out_fail}"
    assert "FAIL:" in out_fail, f"missing FAIL after mutation\n{out_fail}"
