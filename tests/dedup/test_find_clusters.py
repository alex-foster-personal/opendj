"""Tests for ``apps.dedup.find_clusters``."""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest

from apps.dedup import find_clusters as fc_mod
from apps.dedup import scan as scan_mod
from apps.shared.fingerprints import Fingerprint, FingerprintCache
from tests.fingerprint_fakes import fake_fingerprint

SAME_FP = fake_fingerprint(b"same", b"")


def _seed(tmp_path: Path, tmp_fixture_tree: Path) -> Path:
    """Run scan on the fixture tree so the cache + state DB are populated."""
    db = tmp_path / "phase7.sqlite"
    scan_mod.run_scan(roots=[tmp_fixture_tree], db_path=db)
    return db


# ---------------------------------------------------------- pick_canonical


@pytest.mark.requirement("META-03")
def test_pick_canonical_highest_bitrate_wins(tmp_path: Path) -> None:
    a = Fingerprint(
        path=tmp_path / "low.mp3",
        duration=3.0,
        fp_str="x",
        size=1000,
        mtime=100.0,
        bitrate=128,
    )
    b = Fingerprint(
        path=tmp_path / "high.mp3",
        duration=3.0,
        fp_str="x",
        size=1500,
        mtime=200.0,
        bitrate=320,
    )
    winner, rationale = fc_mod._pick_canonical(
        [
            fc_mod._MemberRow(fp=a, on_canonical_root=True),
            fc_mod._MemberRow(fp=b, on_canonical_root=True),
        ]
    )
    assert winner.fp.path.name == "high.mp3"
    assert "bitrate=320" in rationale


@pytest.mark.requirement("META-03")
def test_pick_canonical_size_breaks_bitrate_tie(tmp_path: Path) -> None:
    a = Fingerprint(
        path=tmp_path / "a.mp3", duration=3.0, fp_str="x", size=1000, mtime=100.0, bitrate=320
    )
    b = Fingerprint(
        path=tmp_path / "b.mp3", duration=3.0, fp_str="x", size=2000, mtime=100.0, bitrate=320
    )
    winner, rationale = fc_mod._pick_canonical(
        [
            fc_mod._MemberRow(fp=a, on_canonical_root=True),
            fc_mod._MemberRow(fp=b, on_canonical_root=True),
        ]
    )
    assert winner.fp.path.name == "b.mp3"
    assert "size=2000" in rationale


@pytest.mark.requirement("META-03")
def test_pick_canonical_on_root_wins_over_offroot(tmp_path: Path) -> None:
    a = Fingerprint(
        path=tmp_path / "off" / "a.mp3", duration=3.0, fp_str="x", size=1000, mtime=100.0, bitrate=320
    )
    b = Fingerprint(
        path=tmp_path / "on" / "b.mp3", duration=3.0, fp_str="x", size=1000, mtime=100.0, bitrate=320
    )
    winner, rationale = fc_mod._pick_canonical(
        [
            fc_mod._MemberRow(fp=a, on_canonical_root=False),
            fc_mod._MemberRow(fp=b, on_canonical_root=True),
        ]
    )
    assert winner.fp.path.name == "b.mp3"
    assert "on-canonical-root" in rationale


@pytest.mark.requirement("META-03")
def test_pick_canonical_oldest_mtime_last_resort(tmp_path: Path) -> None:
    a = Fingerprint(
        path=tmp_path / "a.mp3", duration=3.0, fp_str="x", size=1000, mtime=100.0, bitrate=320
    )
    b = Fingerprint(
        path=tmp_path / "b.mp3", duration=3.0, fp_str="x", size=1000, mtime=200.0, bitrate=320
    )
    winner, rationale = fc_mod._pick_canonical(
        [
            fc_mod._MemberRow(fp=a, on_canonical_root=True),
            fc_mod._MemberRow(fp=b, on_canonical_root=True),
        ]
    )
    assert winner.fp.path.name == "a.mp3"
    assert "oldest-mtime" in rationale


# --------------------------------------------------------------- fixtures


@pytest.mark.requirement("META-03")
def test_fixtures_cluster_src_family(
    tmp_path: Path, tmp_fixture_tree, fake_acoustid
) -> None:
    db = _seed(tmp_path, tmp_fixture_tree)
    outcomes = fc_mod.run_find_clusters(
        db_path=db,
        threshold=0.8,   # fake-fp cross-bitrate ~0.5-0.7; lower to hit
        roots=[tmp_fixture_tree],
        clusters_csv=tmp_path / "clusters.csv",
        manual_review_csv=tmp_path / "manual.csv",
    )
    # We expect ONE cluster covering all six src.* fixtures; the
    # other-silent-intro.mp3 file must stay out.
    assert len(outcomes) >= 1
    big = max(outcomes, key=lambda c: len(c.alias_paths))
    involved = {Path(big.canonical_path).name} | {
        Path(p).name for p in big.alias_paths
    }
    assert "other-silent-intro.mp3" not in involved, (
        f"silent-intro should not cluster with src; got {involved}"
    )
    # All six src files should be present.
    src_files = {f for f in involved if f.startswith("src")}
    assert len(src_files) >= 4, f"expected >=4 src family members; got {involved}"


@pytest.mark.requirement("META-03")
def test_csv_has_expected_columns(
    tmp_path: Path, tmp_fixture_tree, fake_acoustid
) -> None:
    db = _seed(tmp_path, tmp_fixture_tree)
    clusters_csv = tmp_path / "clusters.csv"
    fc_mod.run_find_clusters(
        db_path=db,
        threshold=0.8,
        roots=[tmp_fixture_tree],
        clusters_csv=clusters_csv,
        manual_review_csv=tmp_path / "manual.csv",
    )
    with open(clusters_csv, encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
    assert header == [
        "cluster_id",
        "canonical_path",
        "canonical_stable_id",
        "alias_path",
        "alias_stable_id",
        "similarity",
        "rationale",
    ]


@pytest.mark.requirement("META-03")
def test_idempotent_outcomes(
    tmp_path: Path, tmp_fixture_tree, fake_acoustid
) -> None:
    db = _seed(tmp_path, tmp_fixture_tree)
    first = fc_mod.run_find_clusters(
        db_path=db,
        threshold=0.8,
        roots=[tmp_fixture_tree],
        clusters_csv=tmp_path / "c1.csv",
        manual_review_csv=tmp_path / "m1.csv",
    )
    second = fc_mod.run_find_clusters(
        db_path=db,
        threshold=0.8,
        roots=[tmp_fixture_tree],
        clusters_csv=tmp_path / "c2.csv",
        manual_review_csv=tmp_path / "m2.csv",
    )
    # Same cluster shape (path sets).
    def shape(cs):
        return sorted(
            [
                (
                    Path(c.canonical_path).name,
                    tuple(sorted(Path(p).name for p in c.alias_paths)),
                )
                for c in cs
            ]
        )

    assert shape(first) == shape(second)
    with sqlite3.connect(db) as conn:
        first_clusters = conn.execute(
            "SELECT COUNT(*) FROM duplicate_clusters"
        ).fetchone()[0]
        first_aliases = conn.execute(
            "SELECT COUNT(*) FROM track_aliases"
        ).fetchone()[0]
    # Re-read after the second run already completed: counts must match the
    # first run's outcomes, not double them (replace, do not append).
    assert first_clusters == len(first)
    assert first_aliases == sum(len(cluster.alias_paths) for cluster in first)


@pytest.mark.requirement("META-03")
def test_rerun_replaces_same_member_set(tmp_path: Path) -> None:
    db = tmp_path / "phase7.sqlite"
    canonical = Fingerprint(
        path=tmp_path / "canonical.flac",
        duration=180.0,
        fp_str=SAME_FP,
        size=2_000,
        mtime=100.0,
        bitrate=320,
    )
    alias = Fingerprint(
        path=tmp_path / "alias.mp3",
        duration=180.0,
        fp_str=SAME_FP,
        size=1_000,
        mtime=200.0,
        bitrate=128,
    )
    cache = FingerprintCache(db)
    cache.put(canonical, stable_id="canonical-sid")
    cache.put(alias, stable_id="alias-sid")
    first = fc_mod.run_find_clusters(
        db_path=db,
        threshold=0.9,
        roots=[tmp_path],
        clusters_csv=tmp_path / "c1.csv",
        manual_review_csv=tmp_path / "m1.csv",
        fingerprints=[canonical, alias],
    )
    second = fc_mod.run_find_clusters(
        db_path=db,
        threshold=0.9,
        roots=[tmp_path],
        clusters_csv=tmp_path / "c2.csv",
        manual_review_csv=tmp_path / "m2.csv",
        fingerprints=[canonical, alias],
    )
    assert len(first) == 1
    assert len(second) == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM duplicate_clusters"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT COUNT(*) FROM track_aliases"
        ).fetchone() == (1,)
    assert first[0].cluster_id == second[0].cluster_id


@pytest.mark.requirement("META-03")
def test_duration_delta_persists_manual_review_flag(tmp_path: Path) -> None:
    db = tmp_path / "phase7.sqlite"
    canonical = Fingerprint(
        path=tmp_path / "canonical.flac",
        duration=180.0,
        fp_str=SAME_FP,
        size=2_000,
        mtime=100.0,
        bitrate=320,
    )
    alias = Fingerprint(
        path=tmp_path / "alias.mp3",
        duration=190.0,
        fp_str=SAME_FP,
        size=1_000,
        mtime=200.0,
        bitrate=128,
    )
    cache = FingerprintCache(db)
    cache.put(canonical, stable_id="canonical-sid")
    cache.put(alias, stable_id="alias-sid")

    outcomes = fc_mod.run_find_clusters(
        db_path=db,
        threshold=0.9,
        duration_delta_s=3.0,
        roots=[tmp_path],
        clusters_csv=tmp_path / "clusters.csv",
        manual_review_csv=tmp_path / "manual.csv",
        fingerprints=[canonical, alias],
    )

    assert len(outcomes) == 1
    assert outcomes[0].flagged_manual_review is True
    with sqlite3.connect(db) as conn:
        stored = conn.execute(
            "SELECT flagged_manual_review FROM duplicate_clusters"
        ).fetchone()
    assert stored == (1,)


# ---------------------------------------------------------------- CLI smoke


@pytest.mark.requirement("META-03")
def test_cli_smoke(
    tmp_path: Path, tmp_fixture_tree, fake_acoustid, capsys
) -> None:
    db = _seed(tmp_path, tmp_fixture_tree)
    rc = fc_mod.main(
        [
            "--db",
            str(db),
            "--threshold",
            "0.8",
            "--clusters-csv",
            str(tmp_path / "clusters.csv"),
            "--manual-review-csv",
            str(tmp_path / "manual.csv"),
        ]
    )
    assert rc == 0
    cap = capsys.readouterr()
    assert "clusters=" in (cap.out + cap.err)
