"""Tests for :mod:`scripts.moc_refresh`.

Three fixture cases the checker must distinguish, per issue #1473: a present
path, a missing path, and a changed (drifted) threshold. Each case is built
twice -- once in the state the checker should pass, once mutated so the
checker must fail -- so a test that never goes red on the mutated state would
be caught rather than silently passing for the wrong reason (`.claude/rules/
verification.md` "mutate the guard, in BOTH directions").
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import moc_refresh as mod


def _write_repo(tmp_path: Path, *, source_line: str) -> Path:
    repo_root = tmp_path / "repo"
    source = repo_root / "apps" / "widget.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "\n".join(
            [
                "# line 1",
                "# line 2",
                source_line,
                "# line 4",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return repo_root


def _write_moc(repo_root: Path, moc_body: str) -> Path:
    moc_dir = repo_root / "docs" / "moc"
    moc_dir.mkdir(parents=True, exist_ok=True)
    moc_path = moc_dir / "fixture-area.md"
    moc_path.write_text(moc_body, encoding="utf-8")
    return moc_path


# --- case 1: present path ---------------------------------------------------


def test_present_path_passes(tmp_path: Path) -> None:
    repo_root = _write_repo(tmp_path, source_line="WIDGET_MAX = 10")
    _write_moc(repo_root, "The widget module lives at `apps/widget.py:3`.\n")

    rows, drift_count = mod.run(repo_root, repo_root / "docs" / "moc")

    assert drift_count == 0
    assert len(rows) == 1
    assert rows[0].kind == "path"
    assert rows[0].status == mod.OK


def test_missing_path_fails(tmp_path: Path) -> None:
    repo_root = _write_repo(tmp_path, source_line="WIDGET_MAX = 10")
    _write_moc(repo_root, "The widget module lives at `apps/does_not_exist.py`.\n")

    rows, drift_count = mod.run(repo_root, repo_root / "docs" / "moc")

    assert drift_count == 1
    assert rows[0].kind == "path"
    assert rows[0].status == mod.MISSING


def test_path_citation_mutation_present_vs_missing(tmp_path: Path) -> None:
    """The SAME citation text: one repo has the file, one does not.

    Proves the existence check actually fires on the file's real presence,
    not on some property of the citation string alone.
    """
    repo_root_present = _write_repo(tmp_path / "a", source_line="WIDGET_MAX = 10")
    _write_moc(repo_root_present, "`apps/widget.py`\n")
    _, drift_present = mod.run(repo_root_present, repo_root_present / "docs" / "moc")
    assert drift_present == 0

    repo_root_missing = tmp_path / "b" / "repo"
    (repo_root_missing / "docs" / "moc").mkdir(parents=True)
    (repo_root_missing / "docs" / "moc" / "fixture-area.md").write_text(
        "`apps/widget.py`\n", encoding="utf-8"
    )
    _, drift_missing = mod.run(repo_root_missing, repo_root_missing / "docs" / "moc")
    assert drift_missing == 1


# --- case 2: changed (drifted) threshold ------------------------------------


def test_matching_threshold_passes(tmp_path: Path) -> None:
    repo_root = _write_repo(tmp_path, source_line="WIDGET_MAX = 10")
    _write_moc(
        repo_root,
        "The ceiling is `WIDGET_MAX = 10` (`apps/widget.py:3`).\n",
    )

    rows, drift_count = mod.run(repo_root, repo_root / "docs" / "moc")

    assert drift_count == 0
    assert rows[0].kind == "threshold"
    assert rows[0].status == mod.OK


def test_changed_threshold_fails(tmp_path: Path) -> None:
    # Same citation as the passing case above, but the source now disagrees.
    repo_root = _write_repo(tmp_path, source_line="WIDGET_MAX = 25")
    _write_moc(
        repo_root,
        "The ceiling is `WIDGET_MAX = 10` (`apps/widget.py:3`).\n",
    )

    rows, drift_count = mod.run(repo_root, repo_root / "docs" / "moc")

    assert drift_count == 1
    assert rows[0].kind == "threshold"
    assert rows[0].status == mod.DRIFT
    assert "WIDGET_MAX = 25" in rows[0].detail


def test_threshold_mutation_flips_result(tmp_path: Path) -> None:
    """One citation, the source value edited underneath it: OK -> DRIFT.

    This is the mutation the issue asks for: changing the thing under test
    (the source line) must flip the checker's verdict, proving the threshold
    comparison is load-bearing and not a check that always reports OK.
    """
    citation_moc = "The ceiling is `WIDGET_MAX = 10` (`apps/widget.py:3`).\n"

    repo_ok = _write_repo(tmp_path / "ok", source_line="WIDGET_MAX = 10")
    _write_moc(repo_ok, citation_moc)
    _, drift_ok = mod.run(repo_ok, repo_ok / "docs" / "moc")

    repo_drift = _write_repo(tmp_path / "drift", source_line="WIDGET_MAX = 999")
    _write_moc(repo_drift, citation_moc)
    _, drift_drift = mod.run(repo_drift, repo_drift / "docs" / "moc")

    assert drift_ok == 0
    assert drift_drift == 1


def test_threshold_missing_file_reports_missing_not_drift(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "docs" / "moc").mkdir(parents=True)
    (repo_root / "docs" / "moc" / "fixture-area.md").write_text(
        "`WIDGET_MAX = 10` (`apps/nowhere.py:3`)\n", encoding="utf-8"
    )

    rows, drift_count = mod.run(repo_root, repo_root / "docs" / "moc")

    assert drift_count == 1
    assert rows[0].status == mod.MISSING


def test_threshold_line_out_of_bounds_reports_missing(tmp_path: Path) -> None:
    repo_root = _write_repo(tmp_path, source_line="WIDGET_MAX = 10")
    _write_moc(repo_root, "`WIDGET_MAX = 10` (`apps/widget.py:999`)\n")

    rows, drift_count = mod.run(repo_root, repo_root / "docs" / "moc")

    assert drift_count == 1
    assert rows[0].status == mod.MISSING
    assert "out of bounds" in rows[0].detail


# --- root-level citations (no `/` in the path) ------------------------------


def _write_root_file(repo_root: Path, name: str, n_lines: int = 4) -> None:
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / name).write_text(
        "\n".join(f"# {name} line {i}" for i in range(1, n_lines + 1)) + "\n", encoding="utf-8"
    )


def test_root_level_line_citation_is_checked(tmp_path: Path) -> None:
    """`AGENTS.md:2-3` and `justfile:1` are citations; the checker must see them.

    Before this guard the path pattern required a `/`, so a root file could be
    deleted or cited out of bounds and the run still printed OK without even
    listing the citation (Codex P2 on PR #3719).
    """
    repo_root = _write_repo(tmp_path, source_line="WIDGET_MAX = 10")
    _write_root_file(repo_root, "AGENTS.md")
    _write_root_file(repo_root, "justfile")
    _write_moc(
        repo_root, "Rules: `AGENTS.md:2-3`. Recipes: `justfile:1`, and `justfile` overall.\n"
    )

    rows, drift_count = mod.run(repo_root, repo_root / "docs" / "moc")

    assert drift_count == 0
    assert sorted(r.citation for r in rows) == ["`AGENTS.md:2-3`", "`justfile:1`", "`justfile`"]
    assert all(r.status == mod.OK for r in rows)


def test_root_level_citation_mutation_present_vs_missing(tmp_path: Path) -> None:
    """Same citation text; delete the root file -> MISSING, not a silent OK."""
    citation_moc = "Rules: `AGENTS.md:2-3`.\n"

    repo_present = _write_repo(tmp_path / "a", source_line="WIDGET_MAX = 10")
    _write_root_file(repo_present, "AGENTS.md")
    _write_moc(repo_present, citation_moc)
    rows_present, drift_present = mod.run(repo_present, repo_present / "docs" / "moc")

    repo_missing = _write_repo(tmp_path / "b", source_line="WIDGET_MAX = 10")
    _write_moc(repo_missing, citation_moc)
    rows_missing, drift_missing = mod.run(repo_missing, repo_missing / "docs" / "moc")

    assert (drift_present, rows_present[0].status) == (0, mod.OK)
    assert (drift_missing, rows_missing[0].status) == (1, mod.MISSING)


def test_root_level_line_out_of_bounds_reports_missing(tmp_path: Path) -> None:
    repo_root = _write_repo(tmp_path, source_line="WIDGET_MAX = 10")
    _write_root_file(repo_root, "justfile", n_lines=4)
    _write_moc(repo_root, "Recipes: `justfile:3-999`.\n")

    rows, drift_count = mod.run(repo_root, repo_root / "docs" / "moc")

    assert drift_count == 1
    assert rows[0].status == mod.MISSING
    assert "out of bounds" in rows[0].detail


def test_bare_basenames_and_host_ports_are_not_root_citations(tmp_path: Path) -> None:
    """The negative control for the widening: prose that LOOKS root-level.

    None of these exist at the fixture root, so if any were treated as a
    citation the run would report MISSING. Zero rows proves they are ignored,
    and the positive test above proves the same scanner does fire on a real
    root citation.
    """
    repo_root = _write_repo(tmp_path, source_line="WIDGET_MAX = 10")
    _write_moc(
        repo_root,
        "The `state.db` file, `app.py`, `AGENTS.md` bare, `127.0.0.1:8585`, "
        "`localhost:8585`, `1.3.2`, and `AudioContext.currentTime`.\n",
    )

    rows, drift_count = mod.run(repo_root, repo_root / "docs" / "moc")

    assert (rows, drift_count) == ([], 0)


def test_root_level_threshold_pointer_is_not_double_counted(tmp_path: Path) -> None:
    repo_root = _write_repo(tmp_path, source_line="WIDGET_MAX = 10")
    _write_root_file(repo_root, "justfile")
    _write_moc(repo_root, "Line two is `justfile line 2` (`justfile:2`).\n")

    rows, drift_count = mod.run(repo_root, repo_root / "docs" / "moc")

    assert drift_count == 0
    assert [r.kind for r in rows] == ["threshold"]


# --- combined fixture MOC covering all three cases at once ------------------


FIXTURE_MOC = """\
# Fixture area

- Present path: `apps/widget.py`.
- Present path with line: `apps/widget.py:3`.
- Missing path: `apps/gone.py`.
- Matching threshold: `WIDGET_MAX = 10` (`apps/widget.py:3`).
- Drifted threshold: `WIDGET_MAX = 999` (`apps/widget.py:3`).
"""


def test_fixture_moc_all_three_cases_in_one_file(tmp_path: Path) -> None:
    repo_root = _write_repo(tmp_path, source_line="WIDGET_MAX = 10")
    _write_moc(repo_root, FIXTURE_MOC)

    rows, drift_count = mod.run(repo_root, repo_root / "docs" / "moc")

    by_status: dict[str, list[mod.Row]] = {}
    for r in rows:
        by_status.setdefault(r.status, []).append(r)

    assert len(by_status.get(mod.OK, [])) == 3  # 2 present-path + 1 matching threshold
    assert len(by_status.get(mod.MISSING, [])) == 1  # the missing path
    assert len(by_status.get(mod.DRIFT, [])) == 1  # the drifted threshold
    assert drift_count == 2


# --- CLI-level behavior -------------------------------------------------


def test_main_exits_nonzero_on_drift(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo_root = _write_repo(tmp_path, source_line="WIDGET_MAX = 25")
    _write_moc(repo_root, "`WIDGET_MAX = 10` (`apps/widget.py:3`)\n")

    rc = mod.main(["--repo-root", str(repo_root)])

    assert rc == 1
    out = capsys.readouterr().out
    assert "DRIFT" in out
    assert "FAIL" in out


def test_main_exits_zero_when_clean(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo_root = _write_repo(tmp_path, source_line="WIDGET_MAX = 10")
    _write_moc(repo_root, "`WIDGET_MAX = 10` (`apps/widget.py:3`)\n")

    rc = mod.main(["--repo-root", str(repo_root)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "OK" in out


def test_main_missing_moc_dir_is_an_error_not_a_silent_pass(tmp_path: Path) -> None:
    rc = mod.main(["--repo-root", str(tmp_path), "--moc-dir", str(tmp_path / "nope")])
    assert rc == 2
