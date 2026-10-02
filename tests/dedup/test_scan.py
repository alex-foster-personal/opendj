"""Tests for ``apps.dedup.scan``.

Uses the fake acoustid backend from conftest so fpcalc is not required.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.dedup import scan as scan_mod
from apps.shared.fingerprints import ChromaprintMissing, FingerprintCache

# ---------------------------------------------------------------- smoke


@pytest.mark.requirement("META-03")
def test_smoke_fixtures(tmp_path: Path, tmp_fixture_tree, fake_acoustid) -> None:
    db = tmp_path / "cache.sqlite"
    out = scan_mod.run_scan(
        roots=[tmp_fixture_tree], db_path=db, force_recompute=False
    )
    # 7 audio files in the fixture tree (src.wav + src-{320,128,v2}.mp3 +
    # src.m4a + src.flac + other-silent-intro.mp3).
    assert out.scanned == 7
    assert out.computed == 7
    assert out.cache_hits == 0
    assert out.errors == 0

    # Cache now holds 7 rows.
    cache = FingerprintCache(db)
    rows = list(cache.iter_all())
    assert len(rows) == 7


@pytest.mark.requirement("META-03")
def test_incremental_rerun_is_cache_hits(
    tmp_path: Path, tmp_fixture_tree, fake_acoustid
) -> None:
    db = tmp_path / "cache.sqlite"
    first = scan_mod.run_scan(roots=[tmp_fixture_tree], db_path=db)
    assert first.computed == 7

    second = scan_mod.run_scan(roots=[tmp_fixture_tree], db_path=db)
    assert second.computed == 0
    assert second.cache_hits == 7
    assert second.errors == 0


@pytest.mark.requirement("META-03")
def test_force_recompute_hits_every_file(
    tmp_path: Path, tmp_fixture_tree, fake_acoustid
) -> None:
    db = tmp_path / "cache.sqlite"
    _ = scan_mod.run_scan(roots=[tmp_fixture_tree], db_path=db)
    forced = scan_mod.run_scan(
        roots=[tmp_fixture_tree], db_path=db, force_recompute=True
    )
    assert forced.computed == 7
    assert forced.cache_hits == 0


# --------------------------------------------------------- error handling


@pytest.mark.requirement("META-03")
def test_chromaprint_missing_raises(
    tmp_path: Path, tmp_fixture_tree, monkeypatch
) -> None:
    """With no engine build and no fpcalc, scan raises ChromaprintMissing immediately."""
    from apps.shared import fingerprints as fp_mod

    class BadBackend:
        class NoBackendError(Exception):
            pass

        def fingerprint_file(self, _path):
            raise self.NoBackendError("fpcalc missing")

    monkeypatch.setattr(fp_mod, "_engine_binary", lambda: None)
    monkeypatch.setattr(fp_mod, "_require_acoustid", lambda: BadBackend())

    db = tmp_path / "cache.sqlite"
    with pytest.raises(ChromaprintMissing):
        scan_mod.run_scan(roots=[tmp_fixture_tree], db_path=db)


# --------------------------------------------------------------- CLI smoke


@pytest.mark.requirement("META-03")
def test_cli_quiet(
    tmp_path: Path, tmp_fixture_tree, fake_acoustid, capsys
) -> None:
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
    assert "scanned=7" in (captured.out + captured.err)


@pytest.mark.requirement("META-03")
def test_cli_chromaprint_missing_exit_2(
    tmp_path: Path, tmp_fixture_tree, monkeypatch, capsys
) -> None:
    from apps.shared import fingerprints as fp_mod

    class BadBackend:
        class NoBackendError(Exception):
            pass

        def fingerprint_file(self, _path):
            raise self.NoBackendError("fpcalc missing")

    monkeypatch.setattr(fp_mod, "_engine_binary", lambda: None)
    monkeypatch.setattr(fp_mod, "_require_acoustid", lambda: BadBackend())

    rc = scan_mod.main(
        ["--root", str(tmp_fixture_tree), "--db", str(tmp_path / "cli.sqlite"), "--quiet"]
    )
    assert rc == 2
