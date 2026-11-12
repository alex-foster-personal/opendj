"""Tests for ``apps.dedup.scan``.

Uses the fake acoustid backend from conftest so fpcalc is not required.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.dedup import scan as scan_mod
from apps.dedup.db_paths import resolve_dedup_db, resolve_music_roots
from apps.shared import paths
from apps.shared.fingerprints import ChromaprintMissing, FingerprintCache

from tests.dedup.phase7_support import PHASE7_DEDUP_AUDIO_COUNT, patch_chromaprint_backend_missing

# ---------------------------------------------------------- db path default


@pytest.mark.requirement("META-03")
def test_resolve_dedup_db_explicit_and_default(tmp_path: Path) -> None:
    explicit = tmp_path / "dedup.sqlite"
    assert resolve_dedup_db(explicit) == explicit
    assert resolve_dedup_db(None) == paths.DEDUP_FALLBACK_DB


@pytest.mark.requirement("META-03")
def test_resolve_music_roots_explicit_and_default(tmp_path: Path) -> None:
    explicit = [tmp_path / "music"]
    assert resolve_music_roots(explicit) == explicit
    assert resolve_music_roots(None) == paths.MUSIC_ROOTS


# ---------------------------------------------------------------- smoke


@pytest.mark.requirement("META-03")
def test_smoke_fixtures(tmp_path: Path, tmp_fixture_tree, fake_acoustid) -> None:
    db = tmp_path / "cache.sqlite"
    out = scan_mod.run_scan(roots=[tmp_fixture_tree], db_path=db, force_recompute=False)
    assert out.scanned == PHASE7_DEDUP_AUDIO_COUNT
    assert out.computed == PHASE7_DEDUP_AUDIO_COUNT
    assert out.cache_hits == 0
    assert out.errors == 0

    # Cache now holds 7 rows.
    cache = FingerprintCache(db)
    rows = list(cache.iter_all())
    assert len(rows) == PHASE7_DEDUP_AUDIO_COUNT


@pytest.mark.requirement("META-03")
def test_incremental_rerun_is_cache_hits(tmp_path: Path, tmp_fixture_tree, fake_acoustid) -> None:
    db = tmp_path / "cache.sqlite"
    first = scan_mod.run_scan(roots=[tmp_fixture_tree], db_path=db)
    assert first.computed == PHASE7_DEDUP_AUDIO_COUNT

    second = scan_mod.run_scan(roots=[tmp_fixture_tree], db_path=db)
    assert second.computed == 0
    assert second.cache_hits == PHASE7_DEDUP_AUDIO_COUNT
    assert second.errors == 0


@pytest.mark.requirement("META-03")
def test_force_recompute_hits_every_file(tmp_path: Path, tmp_fixture_tree, fake_acoustid) -> None:
    db = tmp_path / "cache.sqlite"
    _ = scan_mod.run_scan(roots=[tmp_fixture_tree], db_path=db)
    forced = scan_mod.run_scan(roots=[tmp_fixture_tree], db_path=db, force_recompute=True)
    assert forced.computed == PHASE7_DEDUP_AUDIO_COUNT
    assert forced.cache_hits == 0


# --------------------------------------------------------- error handling


@pytest.mark.requirement("META-03")
def test_chromaprint_missing_raises(tmp_path: Path, tmp_fixture_tree, monkeypatch) -> None:
    """With no engine build and no fpcalc, scan raises ChromaprintMissing immediately."""
    patch_chromaprint_backend_missing(monkeypatch)

    db = tmp_path / "cache.sqlite"
    with pytest.raises(ChromaprintMissing):
        scan_mod.run_scan(roots=[tmp_fixture_tree], db_path=db)


# --------------------------------------------------------------- CLI smoke


@pytest.mark.requirement("META-03")
def test_cli_quiet(tmp_path: Path, tmp_fixture_tree, fake_acoustid, capsys) -> None:
    rc = scan_mod.main(
        [
            "--root",
            str(tmp_fixture_tree),
            "--db",
            str(tmp_path / "cli.sqlite"),
            "--quiet",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert f"scanned={PHASE7_DEDUP_AUDIO_COUNT}" in (captured.out + captured.err)


@pytest.mark.requirement("META-03")
def test_cli_chromaprint_missing_exit_2(tmp_path: Path, tmp_fixture_tree, monkeypatch, capsys) -> None:
    patch_chromaprint_backend_missing(monkeypatch)

    rc = scan_mod.main(["--root", str(tmp_fixture_tree), "--db", str(tmp_path / "cli.sqlite"), "--quiet"])
    assert rc == 2


# ------------------------------------------------------ library scan basis


@pytest.fixture
def library_state_db(tmp_path: Path) -> Path:
    from apps.shared.state import db as state_db

    db = tmp_path / "state.db"
    state_db.open_rw(db, apply_schema=True).close()
    return db


@pytest.mark.requirement("SETPREP-10643")
def test_tags_only_scan_never_calls_fingerprint_engine(library_state_db: Path, tmp_path: Path, monkeypatch) -> None:
    """[if] basis tags runs [then] compute is never invoked, [else stop]."""
    from unittest.mock import MagicMock

    from apps.dedup import library_scan

    compute = MagicMock(side_effect=AssertionError("fingerprint engine must not run"))
    monkeypatch.setattr(library_scan, "compute", compute)
    db = tmp_path / "dedup.sqlite"
    rows = [("s" * 40, Path("/music/x.mp3")) for _ in range(1000)]
    monkeypatch.setattr(library_scan, "library_tracks", lambda _state: rows)
    monkeypatch.setattr(
        library_scan.tag_clusters,
        "run_tag_clusters",
        lambda **_: library_scan.tag_clusters.TagClusterStats(written=0, precision=None),
    )
    prog = library_scan.run_library_scan(state_db=library_state_db, db_path=db, basis="tags")
    assert prog.basis == "tags"
    assert prog.clusters_fingerprint == 0
    compute.assert_not_called()


@pytest.mark.requirement("SETPREP-10643")
def test_both_basis_runs_tag_pass_after_fingerprints(library_state_db: Path, tmp_path: Path, monkeypatch) -> None:
    """[if] basis is both [then] tag clustering runs after fingerprints, [else stop]."""
    from apps.dedup import library_scan

    order: list[str] = []

    def fake_find_clusters(**_kwargs):
        order.append("fingerprint")
        return []

    def fake_tag_clusters(**_kwargs):
        order.append("tags")
        return library_scan.tag_clusters.TagClusterStats(written=2, precision=None)

    monkeypatch.setattr(library_scan.find_clusters, "run_find_clusters", fake_find_clusters)
    monkeypatch.setattr(library_scan.tag_clusters, "run_tag_clusters", fake_tag_clusters)
    monkeypatch.setattr(library_scan, "_tag_precision", lambda *_args: None)
    monkeypatch.setattr(library_scan, "library_tracks", lambda _state: [])
    prog = library_scan.run_library_scan(
        state_db=library_state_db,
        db_path=tmp_path / "dedup.sqlite",
        basis="both",
    )
    assert order == ["fingerprint", "tags"]
    assert prog.clusters_tags == 2
