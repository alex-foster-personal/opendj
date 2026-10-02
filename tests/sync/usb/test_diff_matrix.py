"""Parametrized diff-matrix harness across every available USB fixture.

The matrix is built dynamically at collection time: every fixture
matching ``rb-usb-export*`` under ``tests/fixtures/`` (directory or
``.extern`` marker) is turned into two parametrized cases — one for
identity round-trip, one for overlay round-trip. Fixtures whose
external host isn't mounted are skipped cleanly rather than failing.

Why this exists
---------------

Before this test the writer-vs-real-export comparison lived in a
bespoke script (``scripts/scratch/diff_usb_exports.py``) that was run by
hand for one fixture (``rb-usb-export-big``). With CAT-06 expanding
and new real-Rekordbox dumps arriving, we need adding a new fixture
to be a one-step operation: drop a ``<name>.extern`` marker (or an
in-repo dir) into ``tests/fixtures/`` and the structural round-trip
test runs against it automatically.

Parametrization strategy
------------------------

We collect fixture names eagerly at import time via
:func:`apps.sync.usb.pioneer.differ.discover_fixtures` — cheap, no
file reads beyond ``iterdir`` on ``tests/fixtures/``. Resolution to a
real path is deferred into each test body so a fixture whose host is
unmounted becomes a ``pytest.skip`` (the default SKIPPED cell in the
pytest report) rather than a collection error.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from apps.sync.usb.pioneer.differ import (
    OPTIONAL_FIXTURE_NAMES,
    diff_snapshots,
    discover_fixtures,
    round_trip_via_writer,
    snapshot_onelibrary,
)
from apps.sync.usb.pioneer.writer_onelibrary import WRITER_AVAILABLE, WRITER_IMPORT_ERROR
from tests.fixtures._resolver import FixtureNotAvailable, fixture_path
from tests.fixtures.conftest import resolve_required_fixture

pytestmark = [
    pytest.mark.requirement("CAT-06"),
    pytest.mark.slow,
    pytest.mark.skipif(
        not WRITER_AVAILABLE,
        reason=(
            f"OneLibrary reader unavailable: {WRITER_IMPORT_ERROR}. "
            "Install the repository dependencies to run the diff-matrix."
        ),
    ),
]


# Collected once at import time. Adding a new rb-usb-export* fixture
# on disk (or a .extern marker) is enough to grow the matrix — no code
# change needed here. If the list is empty the parametrize expansion
# below falls through to a no-op parametrize case that pytest skips.
_FIXTURE_NAMES: list[str] = discover_fixtures("rb-usb-export*")


def _resolve_or_skip(name: str) -> Path:
    """Resolve a fixture name to its ``PIONEER/`` directory.

    ``rb-usb-export-big`` is the one explicitly optional fixture (LaCie-only,
    huge) and skips cleanly when its external host isn't mounted, using
    :class:`FixtureNotAvailable` as the cue. Every other discovered
    ``rb-usb-export*`` name is REQUIRED CAT-06 acceptance coverage and fails
    closed via :func:`resolve_required_fixture` instead, so the matrix can't
    report a non-failing run without actually testing it (AGENTS.md: never
    silently skip acceptance for missing data). A resolved fixture missing
    its expected internal structure (``PIONEER/``, ``exportLibrary.db``)
    always fails hard -- that is a data-integrity problem, not a "missing
    fixture host" one, so it is not gated by ``MDT_ALLOW_MISSING_FIXTURES``.
    """
    if name in OPTIONAL_FIXTURE_NAMES:
        try:
            root = fixture_path(name)
        except FixtureNotAvailable as exc:
            pytest.skip(f"Fixture {name!r} not available on this host: {exc}")
        except FileNotFoundError as exc:
            pytest.skip(f"Fixture {name!r} not found: {exc}")
    else:
        root = resolve_required_fixture(name)
    pioneer = root / "PIONEER"
    if not pioneer.is_dir():
        pytest.fail(f"Fixture {name!r} has no PIONEER/ under {root}")
    db = pioneer / "rekordbox" / "exportLibrary.db"
    if not db.is_file():
        pytest.fail(
            f"Fixture {name!r} has no exportLibrary.db (not a OneLibrary export)"
        )
    return pioneer


@pytest.mark.parametrize("fixture_name", _FIXTURE_NAMES or ["__no_fixtures__"])
def test_identity_round_trip(fixture_name: str, tmp_path: Path) -> None:
    """Writer passthrough must produce a structurally-identical output.

    For a fixture's ``exportLibrary.db`` T, call
    ``write_onelibrary(template=T, output=O, playlists=None,
    track_updates=None)`` and assert the snapshot of O is equal to
    the snapshot of T on every structural axis the differ knows
    about: table set, per-table row counts, playlist name set.
    """
    if fixture_name == "__no_fixtures__":
        pytest.skip("No rb-usb-export* fixtures discovered")

    pioneer = _resolve_or_skip(fixture_name)
    left = snapshot_onelibrary(pioneer / "rekordbox" / "exportLibrary.db")
    out = round_trip_via_writer(pioneer, workdir=tmp_path / "identity")
    right = snapshot_onelibrary(out)

    diff = diff_snapshots(left, right, mode="identity")
    assert diff.verdict == "structurally_identical", (
        f"identity round-trip diverged for {fixture_name!r}: "
        f"{diff.reason}; table_deltas={diff.table_deltas}, "
        f"playlist_additions={sorted(diff.playlist_additions)}, "
        f"playlist_removals={sorted(diff.playlist_removals)}"
    )


@pytest.mark.parametrize("fixture_name", _FIXTURE_NAMES or ["__no_fixtures__"])
def test_overlay_round_trip(fixture_name: str, tmp_path: Path) -> None:
    """Writer + 1 overlay playlist must add exactly +1 playlist, no drift.

    Picks the first up-to-3 content IDs out of the fixture, creates a
    single test playlist ``diff-matrix-test-playlist`` attached to
    those IDs, and asserts:

    * every non-playlist table row count is unchanged;
    * the playlist table grows by exactly +1;
    * the new playlist name appears in the additions set and no
      playlist was removed.

    A fixture with zero content rows is skipped (edge case).
    """
    if fixture_name == "__no_fixtures__":
        pytest.skip("No rb-usb-export* fixtures discovered")

    pioneer = _resolve_or_skip(fixture_name)

    # Pick content IDs safely (via a snapshot — no fixture mutation).
    left = snapshot_onelibrary(pioneer / "rekordbox" / "exportLibrary.db")
    left_rows = left.table_rows()
    content_rows = left_rows.get("content", 0)
    if content_rows == 0:
        pytest.skip(
            f"Fixture {fixture_name!r} has zero content rows; "
            "overlay test requires at least one track to attach"
        )

    # Borrow the matrix runner's id-picker so the test stays in sync
    # with the CLI behaviour (up to 3 IDs; if <3 available, all of
    # them; if 0, we already skipped above).
    from apps.sync.usb.pioneer.differ import _overlay_spec_for_fixture

    overlay = _overlay_spec_for_fixture(pioneer, tmp_path / "pick")
    assert overlay, (
        f"fixture {fixture_name!r} reports {content_rows} content rows "
        "but the id-picker returned no IDs — possible OneLibrary read regression"
    )
    overlay_name = overlay[0][0]

    out = round_trip_via_writer(
        pioneer,
        workdir=tmp_path / "overlay",
        overlay_playlists=overlay,
    )
    right = snapshot_onelibrary(out)
    diff = diff_snapshots(
        left, right,
        mode="overlay",
        expected_playlist_additions={overlay_name},
    )
    assert diff.verdict == "expected_overlay_delta", (
        f"overlay round-trip diverged for {fixture_name!r}: "
        f"{diff.reason}; table_deltas={diff.table_deltas}, "
        f"additions={sorted(diff.playlist_additions)}, "
        f"removals={sorted(diff.playlist_removals)}"
    )


def test_diff_matrix_cli_smoke(tmp_path: Path) -> None:
    """``python -m apps.sync.usb.pioneer diff-matrix`` emits valid output.

    Uses the real subprocess (not in-process) to cover the argparse
    wiring too. We only assert the markdown + JSON are well-formed
    and list every discovered fixture — whether each row is ✅ or ⏭
    depends on host state, so we don't pin a verdict here (the per-
    fixture tests above do that).
    """
    if not _FIXTURE_NAMES:
        pytest.skip("No rb-usb-export* fixtures discovered")

    md_out = tmp_path / "matrix.md"
    json_out = tmp_path / "matrix.json"
    result = subprocess.run(
        [
            sys.executable, "-m", "apps.sync.usb.pioneer", "diff-matrix",
            "--markdown", str(md_out),
            "--json", str(json_out),
        ],
        capture_output=True, text=True, timeout=600,
    )
    # Exit 0 = every row is ok/skipped. Exit 2 = divergence. Either way
    # the output files should exist and be well-formed; the per-fixture
    # tests above assert on verdicts. A non-{0,2} code means argparse
    # or an uncaught exception — that's a real failure.
    assert result.returncode in (0, 2), (
        f"diff-matrix CLI failed unexpectedly: rc={result.returncode}, "
        f"stderr={result.stderr!r}"
    )
    assert md_out.is_file(), f"CLI did not write markdown: {md_out}"
    assert json_out.is_file(), f"CLI did not write JSON: {json_out}"

    md_text = md_out.read_text(encoding="utf-8")
    assert md_text.startswith("# USB Export Diff Matrix"), (
        f"markdown output has unexpected header: {md_text[:80]!r}"
    )
    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert "rows" in payload and isinstance(payload["rows"], list)
    emitted_fixtures = {row["fixture"] for row in payload["rows"]}
    for name in _FIXTURE_NAMES:
        assert name in emitted_fixtures, (
            f"diff-matrix output missing fixture {name!r}: "
            f"got {sorted(emitted_fixtures)}"
        )
