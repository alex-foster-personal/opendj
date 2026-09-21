"""Pure-function tests for :mod:`apps.sync.usb.pioneer.differ`.

The full round-trip matrix in ``test_diff_matrix.py`` only runs when
rbox is installed and at least one fixture resolves. These tests
exercise the *pure* logic paths that can run cheaply anywhere:

* ``diff_snapshots`` — every verdict branch under both modes,
  including snapshot-error propagation, missing-table / removal
  detection, and both success verdicts.
* ``OneLibrarySchemaSnapshot`` / ``OneLibraryTableStats`` dataclass
  ergonomics (``table_rows`` + ``distinct_ids`` default).
* ``MatrixRow.to_dict`` round-trip.
* ``render_matrix_markdown`` / ``render_matrix_json`` formatting.
* ``_fmt_set_delta`` — truncation at the ``limit`` threshold.
* ``_head_hex`` / ``_sha256`` / ``_relative_files`` file helpers.
* ``build_report`` fallback when ``exportLibrary.db`` is absent
  (exercises the legacy markdown path without touching rbox).
* ``diff_pair_cli`` (stdout + file output flow).
* ``discover_fixtures`` + ``run_matrix`` error rows for unavailable
  and malformed fixtures (no rbox required for either path).
* ``round_trip_via_writer`` missing-template error.
* ``snapshot_onelibrary`` handles the absent-rbox module gracefully.

Requirement: CAT-06.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from apps.sync.usb.pioneer import differ

pytestmark = pytest.mark.requirement("CAT-06")


# --------------------------------------------------------------------------- #
# Snapshot construction helpers
# --------------------------------------------------------------------------- #


def _snap(
    *,
    path: str = "/x/exportLibrary.db",
    rows: dict[str, int] | None = None,
    playlists: tuple[str, ...] = (),
    error: str | None = None,
) -> differ.OneLibrarySchemaSnapshot:
    rows = rows or {}
    tables = tuple(
        differ.OneLibraryTableStats(table_name=n, row_count=c)
        for n, c in sorted(rows.items())
    )
    return differ.OneLibrarySchemaSnapshot(
        path=Path(path), tables=tables, playlist_names=playlists, error=error,
    )


# --------------------------------------------------------------------------- #
# OneLibrarySchemaSnapshot / OneLibraryTableStats
# --------------------------------------------------------------------------- #


def test_schema_snapshot_table_rows_roundtrip() -> None:
    snap = _snap(rows={"content": 100, "playlist": 5})
    assert snap.table_rows() == {"content": 100, "playlist": 5}
    # distinct_ids defaults to None.
    assert all(t.distinct_ids is None for t in snap.tables)


def test_table_stats_accept_distinct_ids() -> None:
    ts = differ.OneLibraryTableStats(
        table_name="content", row_count=3, distinct_ids=3
    )
    assert ts.distinct_ids == 3


# --------------------------------------------------------------------------- #
# diff_snapshots — mode validation
# --------------------------------------------------------------------------- #


def test_diff_snapshots_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="mode must be"):
        differ.diff_snapshots(_snap(), _snap(), mode="nope")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# diff_snapshots — identity mode branches
# --------------------------------------------------------------------------- #


def test_identity_structurally_identical_verdict() -> None:
    left = _snap(rows={"content": 10, "playlist": 2}, playlists=("a", "b"))
    right = _snap(rows={"content": 10, "playlist": 2}, playlists=("a", "b"))
    diff = differ.diff_snapshots(left, right, mode="identity")
    assert diff.verdict == "structurally_identical"
    assert diff.reason == "structurally identical"
    assert diff.playlist_additions == frozenset()
    assert diff.playlist_removals == frozenset()


def test_identity_detects_missing_tables() -> None:
    left = _snap(rows={"content": 10, "playlist": 2})
    right = _snap(rows={"playlist": 2})  # content missing
    diff = differ.diff_snapshots(left, right, mode="identity")
    assert diff.verdict == "unexpected_divergence"
    assert "content" in diff.reason
    assert diff.missing_tables == frozenset({"content"})


def test_identity_detects_row_count_delta() -> None:
    left = _snap(rows={"content": 10, "playlist": 2})
    right = _snap(rows={"content": 12, "playlist": 2})
    diff = differ.diff_snapshots(left, right, mode="identity")
    assert diff.verdict == "unexpected_divergence"
    assert diff.table_deltas["content"] == 2
    assert "row count deltas" in diff.reason


def test_identity_detects_playlist_set_drift() -> None:
    left = _snap(
        rows={"content": 3, "playlist": 1},
        playlists=("alpha",),
    )
    right = _snap(
        rows={"content": 3, "playlist": 1},
        playlists=("beta",),
    )
    diff = differ.diff_snapshots(left, right, mode="identity")
    assert diff.verdict == "unexpected_divergence"
    assert diff.playlist_additions == frozenset({"beta"})
    assert diff.playlist_removals == frozenset({"alpha"})
    assert "playlist set differs" in diff.reason


def test_diff_propagates_snapshot_error() -> None:
    left = _snap(error="rbox unavailable")
    right = _snap(rows={"content": 1})
    diff = differ.diff_snapshots(left, right, mode="identity")
    assert diff.verdict == "unexpected_divergence"
    assert "snapshot error" in diff.reason


# --------------------------------------------------------------------------- #
# diff_snapshots — overlay mode branches
# --------------------------------------------------------------------------- #


def test_overlay_expected_delta_verdict() -> None:
    left = _snap(
        rows={"content": 10, "playlist": 2}, playlists=("a", "b"),
    )
    right = _snap(
        rows={"content": 10, "playlist": 3}, playlists=("a", "b", "c"),
    )
    diff = differ.diff_snapshots(
        left, right, mode="overlay",
        expected_playlist_additions=["c"],
    )
    assert diff.verdict == "expected_overlay_delta"
    assert "overlay added 1" in diff.reason


def test_overlay_rejects_when_tables_missing() -> None:
    left = _snap(rows={"content": 1, "playlist": 1})
    right = _snap(rows={"playlist": 1})
    diff = differ.diff_snapshots(left, right, mode="overlay")
    assert diff.verdict == "unexpected_divergence"
    assert "table set differs under overlay" in diff.reason


def test_overlay_rejects_when_playlist_removed() -> None:
    left = _snap(rows={"playlist": 2}, playlists=("a", "b"))
    right = _snap(rows={"playlist": 1}, playlists=("a",))
    diff = differ.diff_snapshots(left, right, mode="overlay")
    assert diff.verdict == "unexpected_divergence"
    assert "must not remove playlists" in diff.reason


def test_overlay_rejects_when_non_playlist_table_changes() -> None:
    left = _snap(rows={"content": 10, "playlist": 2})
    right = _snap(rows={"content": 11, "playlist": 3})
    diff = differ.diff_snapshots(left, right, mode="overlay")
    assert diff.verdict == "unexpected_divergence"
    assert "non-playlist tables changed" in diff.reason


def test_overlay_rejects_wrong_playlist_delta() -> None:
    left = _snap(rows={"playlist": 2}, playlists=("a", "b"))
    right = _snap(rows={"playlist": 4}, playlists=("a", "b", "c", "d"))
    diff = differ.diff_snapshots(
        left, right, mode="overlay",
        expected_playlist_additions=["c"],  # expecting 1, got 2
    )
    assert diff.verdict == "unexpected_divergence"
    assert "playlist delta" in diff.reason


def test_overlay_rejects_unexpected_additions() -> None:
    left = _snap(rows={"playlist": 1}, playlists=("a",))
    right = _snap(rows={"playlist": 2}, playlists=("a", "d"))
    diff = differ.diff_snapshots(
        left, right, mode="overlay",
        expected_playlist_additions=["c"],  # same count, different name
    )
    assert diff.verdict == "unexpected_divergence"
    assert "playlist additions" in diff.reason


def test_overlay_accepts_zero_expected_additions() -> None:
    """No expected additions + empty actual → expected_overlay_delta."""
    left = _snap(rows={"playlist": 1}, playlists=("a",))
    right = _snap(rows={"playlist": 1}, playlists=("a",))
    diff = differ.diff_snapshots(left, right, mode="overlay")
    assert diff.verdict == "expected_overlay_delta"


# --------------------------------------------------------------------------- #
# File helpers — _head_hex, _sha256, _relative_files
# --------------------------------------------------------------------------- #


def test_head_hex_returns_first_n_bytes_as_hex(tmp_path: Path) -> None:
    f = tmp_path / "a.bin"
    f.write_bytes(b"hello world!")
    assert differ._head_hex(f, n=5) == b"hello".hex()


def test_sha256_matches_stdlib(tmp_path: Path) -> None:
    f = tmp_path / "a.bin"
    data = b"lorem ipsum dolor sit amet"
    f.write_bytes(data)
    assert differ._sha256(f) == hashlib.sha256(data).hexdigest()


def test_relative_files_skips_dotunderscore_and_dirs(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "._ignored").write_text("skip")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("y")
    rels = differ._relative_files(tmp_path)
    assert Path("a.txt") in rels
    assert Path("sub/b.txt") in rels
    assert Path("._ignored") not in rels


# --------------------------------------------------------------------------- #
# Legacy markdown helpers
# --------------------------------------------------------------------------- #


def test_fmt_set_delta_truncates_at_limit() -> None:
    many = [Path(f"/x/file-{i}.txt") for i in range(25)]
    lines = differ._fmt_set_delta("X", many, [], limit=3)
    blob = "\n".join(lines)
    assert "Only in A: **25**" in blob
    assert "+22 more" in blob  # 25 - 3


def test_fmt_set_delta_empty_sides() -> None:
    lines = differ._fmt_set_delta("X", [], [])
    blob = "\n".join(lines)
    assert "Only in A: **0**" in blob
    assert "Only in B: **0**" in blob


def test_diff_onelibrary_markdown_includes_row_table() -> None:
    left = _snap(
        path="/a/exportLibrary.db",
        rows={"content": 10, "playlist": 2},
        playlists=("a", "b"),
    )
    right = _snap(
        path="/b/exportLibrary.db",
        rows={"content": 11, "playlist": 3},
        playlists=("a", "b", "c"),
    )
    out = "\n".join(differ._diff_onelibrary_markdown(
        left, right, 100, "deadbeef", 100, "deadbeef"
    ))
    assert "Row counts" in out
    assert "| `content` |" in out
    assert "Playlists: A=2, B=3" in out
    assert "Added in B" in out


def test_diff_onelibrary_markdown_surfaces_errors() -> None:
    left = _snap(error="A broke")
    right = _snap(error="B broke")
    out = "\n".join(differ._diff_onelibrary_markdown(
        left, right, 0, "00", 0, "00"
    ))
    assert "A could not be fully opened" in out
    assert "B could not be fully opened" in out


def test_diff_pdb_table_row_for_missing_files(tmp_path: Path) -> None:
    """``_diff_pdb`` emits a '(missing)' cell when a PDB is absent."""
    (tmp_path / "a-root" / "rekordbox").mkdir(parents=True)
    (tmp_path / "b-root" / "rekordbox").mkdir(parents=True)
    # Only A has export.pdb. B missing → '(missing)' cell.
    (tmp_path / "a-root" / "rekordbox" / "export.pdb").write_bytes(b"pdb")
    out = "\n".join(differ._diff_pdb(
        tmp_path / "a-root", tmp_path / "b-root"
    ))
    assert "(missing)" in out
    assert "export.pdb" in out


def test_diff_pdb_equal_files_render_checkmark(tmp_path: Path) -> None:
    for root in ("a", "b"):
        (tmp_path / root / "rekordbox").mkdir(parents=True)
        (tmp_path / root / "rekordbox" / "export.pdb").write_bytes(b"payload-x")
        (tmp_path / root / "rekordbox" / "exportExt.pdb").write_bytes(b"payload-y")
    out = "\n".join(differ._diff_pdb(tmp_path / "a", tmp_path / "b"))
    assert "✅" in out


def test_diff_anlz_skips_when_usbanlz_missing(tmp_path: Path) -> None:
    out = "\n".join(differ._diff_anlz(tmp_path / "a", tmp_path / "b"))
    assert "Skipped" in out


def test_diff_anlz_notes_no_common_dirs(tmp_path: Path) -> None:
    (tmp_path / "a" / "USBANLZ").mkdir(parents=True)
    (tmp_path / "b" / "USBANLZ").mkdir(parents=True)
    out = "\n".join(differ._diff_anlz(tmp_path / "a", tmp_path / "b"))
    assert "No common ANLZ0000.DAT" in out


# --------------------------------------------------------------------------- #
# build_report
# --------------------------------------------------------------------------- #


def test_build_report_falls_back_when_onelibrary_missing(tmp_path: Path) -> None:
    """With no exportLibrary.db on either side, build_report still runs.

    Ensures the fallback branch (lines 679-685 in the module) is hit.
    """
    a = tmp_path / "a"
    b = tmp_path / "b"
    (a / "rekordbox").mkdir(parents=True)
    (b / "rekordbox").mkdir(parents=True)
    # Drop a file in each root so _relative_files yields non-empty sets.
    (a / "marker.txt").write_text("A")
    (b / "marker.txt").write_text("B")
    report = differ.build_report(a, b, title="smoke")
    assert "# smoke" in report
    assert "File presence" in report
    assert "exportLibrary.db" in report
    # Missing-OneLibrary fallback line is present.
    assert "A present: False" in report or "B present: False" in report


# --------------------------------------------------------------------------- #
# diff_pair_cli
# --------------------------------------------------------------------------- #


def test_diff_pair_cli_writes_output_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    (a / "rekordbox").mkdir(parents=True)
    (b / "rekordbox").mkdir(parents=True)
    out = tmp_path / "report.md"
    rc = differ.diff_pair_cli(
        ["--a", str(a), "--b", str(b), "--out", str(out), "--title", "t"]
    )
    assert rc == 0
    assert out.is_file()
    assert out.read_text(encoding="utf-8").startswith("# t\n")


def test_diff_pair_cli_writes_stdout_when_no_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    (a / "rekordbox").mkdir(parents=True)
    (b / "rekordbox").mkdir(parents=True)
    rc = differ.diff_pair_cli(["--a", str(a), "--b", str(b)])
    assert rc == 0
    assert "# USB export diff" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# MatrixRow + renderers
# --------------------------------------------------------------------------- #


def test_matrix_row_to_dict_exposes_all_fields() -> None:
    row = differ.MatrixRow(
        fixture="f", mode="identity", status="ok",
        verdict="structurally_identical",
        tracks=100, playlists=5,
        delta_content=0, delta_playlist=0, reason="ok",
    )
    d = row.to_dict()
    assert d == {
        "fixture": "f", "mode": "identity", "status": "ok",
        "verdict": "structurally_identical",
        "tracks": 100, "playlists": 5,
        "delta_content": 0, "delta_playlist": 0,
        "reason": "ok",
    }


def test_render_matrix_markdown_formats_signs_and_status() -> None:
    rows = [
        differ.MatrixRow(
            fixture="f1", mode="identity", status="ok",
            verdict="structurally_identical",
            tracks=100, playlists=5, delta_content=0, delta_playlist=0,
            reason="",
        ),
        differ.MatrixRow(
            fixture="f2", mode="overlay", status="ok",
            verdict="expected_overlay_delta",
            tracks=100, playlists=5, delta_content=0, delta_playlist=1,
            reason="added 1",
        ),
        differ.MatrixRow(
            fixture="f3", mode="—", status="skipped",
            verdict="skipped",
            tracks=None, playlists=None,
            delta_content=None, delta_playlist=None,
            reason="host unmounted",
        ),
        differ.MatrixRow(
            fixture="f4", mode="—", status="error",
            verdict="error",
            tracks=None, playlists=None,
            delta_content=None, delta_playlist=None,
            reason="boom",
        ),
    ]
    when = _dt.datetime(2026, 4, 17, 5, 0, tzinfo=_dt.UTC)
    md = differ.render_matrix_markdown(rows, now=when)
    assert "Generated: 2026-04-17T05:00:00+00:00" in md
    assert "| `f1` | identity" in md
    assert "| `f2` | overlay" in md
    assert "+1" in md  # signed delta format
    assert "⏭" in md and "host unmounted" in md
    assert "⚠️" in md and "boom" in md


def test_render_matrix_json_roundtrips() -> None:
    row = differ.MatrixRow(
        fixture="f", mode="identity", status="ok",
        verdict="structurally_identical",
        tracks=1, playlists=1, delta_content=0, delta_playlist=0,
        reason="",
    )
    out = json.loads(differ.render_matrix_json([row]))
    assert out == {"rows": [row.to_dict()]}


# --------------------------------------------------------------------------- #
# discover_fixtures + run_matrix error paths
# --------------------------------------------------------------------------- #


def test_discover_fixtures_picks_up_dirs_and_extern_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = tmp_path / "fixtures"
    fake.mkdir()
    (fake / "rb-usb-export-a").mkdir()
    (fake / "rb-usb-export-b.extern").write_text("{}")
    (fake / "other-thing").mkdir()
    # .extern-suffixed DIR must be ignored (it's not the canonical form).
    (fake / "rb-usb-export-c.extern").mkdir()

    # Reload the resolver module reference so discover_fixtures sees the stub.
    import tests.fixtures._resolver as resolver  # type: ignore[import-not-found]
    monkeypatch.setattr(resolver, "FIXTURES_ROOT", fake)

    names = differ.discover_fixtures("rb-usb-export*")
    # 'a' (dir) + 'b' (.extern marker) should be discovered; 'other-thing'
    # does not match; 'c.extern' is a dir so is filtered out (dir path).
    assert set(names) >= {"rb-usb-export-a", "rb-usb-export-b"}
    assert "other-thing" not in names


@contextmanager
def _env_var(name: str, value: str) -> Iterator[None]:
    """Set a real environment variable for the block, then restore it.

    AGENTS.md forbids mocks/monkeypatching/fabricated application state in
    tests: the resolver's external-host override is a real, documented
    production configuration surface (``MUX_FIXTURE_HOST``), so driving it
    through the actual environment is the production configuration path,
    not a fake.
    """
    previous = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


def test_run_matrix_returns_error_row_for_unavailable_required_fixture(
    tmp_path: Path,
) -> None:
    """A REQUIRED fixture whose host is unmounted -> an error row, not skipped.

    PR #718 review: this CLI-facing matrix used to translate any
    ``FixtureNotAvailable`` into a "skipped" row, which let
    ``_cmd_diff_matrix`` exit 0 without ever having exercised a required
    CAT-06 fixture. Only :data:`differ.OPTIONAL_FIXTURE_NAMES` may skip;
    every other name must fail closed as an "error" row instead.

    Exercised via the real, already-committed ``rb-usb-export-big.extern``
    marker and the real, documented ``MUX_FIXTURE_HOST`` override --
    :func:`differ._resolve_matrix_fixture` is called directly with
    ``optional=False`` (bypassing this name's real OPTIONAL_FIXTURE_NAMES
    classification on purpose, to exercise the required-fixture branch) so
    no resolver state or FIXTURES_ROOT needs patching (AGENTS.md's
    no-mocks rule).
    """
    with _env_var("MUX_FIXTURE_HOST", str(tmp_path / "not-mounted")):
        _root, row = differ._resolve_matrix_fixture("rb-usb-export-big", optional=False)

    assert row is not None
    assert row.status == "error"
    assert row.fixture == "rb-usb-export-big"
    assert "not mounted" in row.reason


def test_run_matrix_skips_optional_fixture_when_unavailable(
    tmp_path: Path,
) -> None:
    """The one genuinely optional fixture still skips cleanly.

    Full production path, no test-only shortcuts: ``run_matrix()``
    discovers this name from the real, already-committed
    ``rb-usb-export-big.extern`` marker under the repo's real
    ``FIXTURES_ROOT`` (left untouched), classifies it through the real
    :data:`differ.OPTIONAL_FIXTURE_NAMES`, and resolves it through the
    real, documented ``MUX_FIXTURE_HOST`` override -- no monkeypatching,
    no scratch fixtures-root layout.
    """
    with _env_var("MUX_FIXTURE_HOST", str(tmp_path / "not-mounted")):
        rows = differ.run_matrix(work_root=tmp_path / "wk", fixture_glob="rb-usb-export-big")

    assert len(rows) == 1
    assert rows[0].status == "skipped"
    assert rows[0].fixture == "rb-usb-export-big"


def test_run_matrix_returns_error_row_when_fixture_missing_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tests.fixtures._resolver as resolver  # type: ignore[import-not-found]

    def _raise(_name):
        raise FileNotFoundError("bad path")
    monkeypatch.setattr(differ, "discover_fixtures", lambda *_a, **_k: ["bust"])
    monkeypatch.setattr(resolver, "fixture_path", _raise)

    rows = differ.run_matrix(work_root=tmp_path / "wk")
    assert len(rows) == 1
    assert rows[0].status == "error"
    assert rows[0].verdict == "error"


def test_verify_fixture_contract_detects_mismatch_against_real_contract(
    tmp_path: Path,
) -> None:
    """A fixture root that fails its content contract raises, for real content.

    PR #718 review: the CLI matrix bypassed contract verification
    entirely, so a stale/regenerated ``MUX_FIXTURE_HOST`` tree could
    report a clean run without ever being checked against
    ``tests/fixtures/<name>.contract.json`` -- ``_resolve_matrix_fixture``
    converts that into an "error" row via the exact same
    try/except-then-row shape already covered end-to-end by the
    required-fixture-unavailable test above.

    This test covers the part that shape wraps: real detection. It uses
    the real, already-committed ``tests/fixtures/rb-usb-export.contract.json``
    (unmodified -- canonical fixture sources are immutable) against a
    caller-supplied ``root`` directory that deliberately does not match
    it. ``root`` is ``verify_fixture_contract``'s own, already-supported
    parameter, not a monkeypatch: no ``FIXTURES_ROOT`` or resolver state
    is patched, and the real canonical ``rb-usb-export`` fixture is never
    touched (AGENTS.md's no-mocks and immutable-fixtures rules).
    """
    from tests.fixtures._resolver import (  # type: ignore[import-not-found]
        FixtureContractMismatch,
        verify_fixture_contract,
    )

    mismatched_root = tmp_path / "not-the-real-fixture"
    mismatched_root.mkdir()
    (mismatched_root / "unexpected.bin").write_text("wrong content", encoding="utf-8")

    with pytest.raises(FixtureContractMismatch):
        verify_fixture_contract("rb-usb-export", mismatched_root)


def test_run_matrix_flags_missing_pioneer_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A root that has no ``PIONEER/`` subdir → error row."""
    import tests.fixtures._resolver as resolver  # type: ignore[import-not-found]

    root = tmp_path / "f"
    root.mkdir()
    monkeypatch.setattr(differ, "discover_fixtures", lambda *_a, **_k: ["f"])
    monkeypatch.setattr(resolver, "fixture_path", lambda _n: root)

    rows = differ.run_matrix(work_root=tmp_path / "wk")
    assert len(rows) == 1
    assert rows[0].status == "error"
    assert "no PIONEER/" in rows[0].reason


def test_run_matrix_flags_missing_exportlibrary_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PIONEER/ exists but exportLibrary.db is absent → error row."""
    import tests.fixtures._resolver as resolver  # type: ignore[import-not-found]

    root = tmp_path / "f"
    (root / "PIONEER" / "rekordbox").mkdir(parents=True)
    # No exportLibrary.db.
    monkeypatch.setattr(differ, "discover_fixtures", lambda *_a, **_k: ["f"])
    monkeypatch.setattr(resolver, "fixture_path", lambda _n: root)

    rows = differ.run_matrix(work_root=tmp_path / "wk")
    assert len(rows) == 1
    assert rows[0].status == "error"
    assert "exportLibrary.db" in rows[0].reason


# --------------------------------------------------------------------------- #
# round_trip_via_writer error paths
# --------------------------------------------------------------------------- #


def test_round_trip_via_writer_requires_exportlibrary_db(tmp_path: Path) -> None:
    pioneer = tmp_path / "PIONEER"
    (pioneer / "rekordbox").mkdir(parents=True)
    # No exportLibrary.db → FileNotFoundError.
    with pytest.raises(FileNotFoundError, match="OneLibrary template"):
        differ.round_trip_via_writer(pioneer, workdir=tmp_path / "wk")


# --------------------------------------------------------------------------- #
# snapshot_onelibrary when rbox is absent
# --------------------------------------------------------------------------- #


def test_snapshot_onelibrary_returns_error_when_rbox_import_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``import rbox`` raises, we get back an error-carrying snapshot."""
    # Force the import to fail by stubbing sys.modules before the local import.
    monkeypatch.setitem(sys.modules, "rbox", None)
    db = tmp_path / "exportLibrary.db"
    db.write_bytes(b"placeholder")
    snap = differ.snapshot_onelibrary(db)
    assert snap.error is not None
    assert snap.tables == ()
    assert snap.playlist_names == ()


# --------------------------------------------------------------------------- #
# _overlay_spec_for_fixture when rbox absent / DB missing
# --------------------------------------------------------------------------- #


def test_overlay_spec_for_fixture_handles_missing_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pioneer = tmp_path / "PIONEER"
    (pioneer / "rekordbox").mkdir(parents=True)
    # Force rbox import inside helper to succeed at top but fail on open.
    spec = differ._overlay_spec_for_fixture(pioneer, tmp_path / "wk")
    assert spec == []


def test_overlay_spec_for_fixture_returns_empty_when_rbox_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "rbox", None)
    pioneer = tmp_path / "PIONEER"
    (pioneer / "rekordbox").mkdir(parents=True)
    (pioneer / "rekordbox" / "exportLibrary.db").write_bytes(b"x")
    spec = differ._overlay_spec_for_fixture(pioneer, tmp_path / "wk")
    assert spec == []
