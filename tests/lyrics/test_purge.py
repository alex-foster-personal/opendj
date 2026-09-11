"""The licensing lever: one provider's lyrics leave every store at once.

Policy mode is an import-time snapshot (``apps.cloud.policy.CFG``), so these
tests flip it through the explicit ``use_cloud_mode`` / ``use_local_mode``
helpers in ``conftest`` (env var AND ``load_policy()`` re-run), stated here
rather than implied.

- if a purge clears the row but leaves the local artifact, or clears both but
  leaves the R2 object, then the licensed text is still on disk somewhere and
  the lever is a lie -- broken
- if the row is hard-deleted instead of tombstoned then the next sync pulls it
  back from a peer -- broken
- if the rows do not each get their own stamped_transaction then an
  interrupted purge leaves rows tombstoned with no changelog entry to push --
  broken
- if any count is inferred from another (files_removed assumed equal to
  rows_tombstoned, objects assumed deleted because a delete was attempted)
  then the report cannot be used as evidence -- broken
- if --dry-run writes anything at all then the rehearsal is the performance --
  broken
- if a second purge over the same prefix tombstones rows again then the purge
  is not idempotent and the counts double-report -- broken
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.cloud import asset_store
from apps.cloud.config import CloudConfig
from apps.lyrics import artifacts, karaoke_cache, purge, store
from apps.lyrics.__main__ import main as lyrics_cli
from apps.lyrics.karaoke_cache import PIPELINE_VERSION
from apps.shared.state import sync_stamp

from .conftest import (
    InMemoryAssetS3,
    seed_stamped_policy,
    seed_track,
    use_cloud_mode,
    use_local_mode,
    word,
)

WORDS = [word("one", start_s=1.0, end_s=1.2), word("two", start_s=1.5, line_final=True)]


#-----------------------------------------------------------------------------
# helpers
#-----------------------------------------------------------------------------
def _seed_verdict(
    conn: sqlite3.Connection,
    data_dir: Path,
    stable_id: str,
    *,
    source: str,
    s3: InMemoryAssetS3 | None = None,
    cfg: CloudConfig | None = None,
) -> artifacts.WordsArtifact:
    """One track with a words artifact on disk and a live verdict row."""
    seed_track(conn, stable_id)
    artifact = artifacts.produce_words_artifact(
        conn,
        data_dir=data_dir,
        stable_id=stable_id,
        source=source,
        words=WORDS,
        s3=s3,
        cfg=cfg,
    )
    store.upsert_verdict(
        conn,
        stable_id=stable_id,
        verdict="vocal",
        coverage_pct=80.0,
        source=source,
        language_iso3="eng",
        n_words=artifact.n_words,
        n_lines=artifact.n_lines,
        pct_witness_red=None,
        pipeline_version=PIPELINE_VERSION,
        words_content_hash=artifact.content_hash,
        computed_at="2026-09-01T00:00:00.000000+00:00",
        resurrect=False,
    )
    return artifact


def _changelog_pks(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT row_pk FROM local_changelog WHERE table_name = 'lyric_verdict' "
            "ORDER BY rowid"
        )
    ]


def _purge(
    conn: sqlite3.Connection,
    data_dir: Path,
    *,
    prefix: str = "musixmatch",
    s3: InMemoryAssetS3 | None = None,
    cfg: CloudConfig | None = None,
    dry_run: bool = False,
) -> purge.PurgeReport:
    return purge.purge_by_source(
        conn,
        data_dir=data_dir,
        source_prefix=prefix,
        s3=s3,
        cfg=cfg,
        dry_run=dry_run,
    )


#-----------------------------------------------------------------------------
# local mode: row + file, and an honest word about R2
#-----------------------------------------------------------------------------
def test_local_mode_tombstones_every_matching_row_and_removes_its_file(
    conn: sqlite3.Connection, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_local_mode(monkeypatch)
    first = _seed_verdict(conn, data_dir, "sid-1", source="musixmatch api")
    second = _seed_verdict(conn, data_dir, "sid-2", source="musixmatch bulk")
    kept = _seed_verdict(conn, data_dir, "sid-3", source="lrclib get")

    report = _purge(conn, data_dir)

    assert report.rows_matched == 2
    assert report.rows_tombstoned == 2
    assert sorted(report.stable_ids) == ["sid-1", "sid-2"]
    assert (report.files_removed, report.files_absent) == (2, 0)
    assert not first.path.exists() and not second.path.exists()
    assert kept.path.is_file(), "a non-matching source keeps its words"
    assert store.get_verdict(conn, "sid-1") is None
    assert store.get_verdict(conn, "sid-3") is not None


def test_the_row_is_tombstoned_not_deleted(
    conn: sqlite3.Connection, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hard DELETE would be re-pushed by the next peer that still has it."""
    use_local_mode(monkeypatch)
    _seed_verdict(conn, data_dir, "sid-1", source="musixmatch api")
    _purge(conn, data_dir)
    row = conn.execute(
        "SELECT deleted_at, updated_at, origin_device_id FROM lyric_verdict "
        "WHERE stable_id = 'sid-1'"
    ).fetchone()
    assert row is not None, "the row must still exist, carrying its tombstone"
    assert row[0] is not None
    assert row[0] == row[1] == sync_stamp.to_canonical(str(row[1]))
    assert row[2] == sync_stamp.local_machine_id(conn)


def test_each_purged_row_is_its_own_stamped_transaction(
    conn: sqlite3.Connection, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One changelog entry per row, so an interrupted purge is resumable."""
    use_local_mode(monkeypatch)
    _seed_verdict(conn, data_dir, "sid-1", source="musixmatch api")
    _seed_verdict(conn, data_dir, "sid-2", source="musixmatch bulk")
    before = _changelog_pks(conn)

    _purge(conn, data_dir)

    after = _changelog_pks(conn)
    assert after[: len(before)] == before, "the purge appended, it did not rewrite"
    assert sorted(after[len(before) :]) == sorted(
        sync_stamp.encode_row_pk((stable_id,)) for stable_id in ("sid-1", "sid-2")
    )
    assert not conn.in_transaction, "every write owns and closes its transaction"


def test_local_mode_says_why_r2_was_left_alone(
    conn: sqlite3.Connection, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silence about R2 would read as 'the object is gone'. It is not."""
    use_local_mode(monkeypatch)
    _seed_verdict(conn, data_dir, "sid-1", source="musixmatch api")
    report = _purge(conn, data_dir)
    assert report.r2_skipped_reason == purge.LOCAL_MODE_R2_REASON
    assert (report.objects_deleted, report.objects_absent) == (0, 0)
    rendered = purge.format_report(report)
    assert "R2 untouched" in rendered
    assert purge.LOCAL_MODE_R2_REASON in rendered


#-----------------------------------------------------------------------------
# cloud mode: the object goes too
#-----------------------------------------------------------------------------
def test_cloud_mode_deletes_the_object_and_a_head_no_longer_finds_it(
    conn: sqlite3.Connection,
    data_dir: Path,
    fake_s3: InMemoryAssetS3,
    cfg: CloudConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_cloud_mode(monkeypatch)
    seed_stamped_policy(conn, asset_kind="karaoke_words", mode="pinned")
    artifact = _seed_verdict(
        conn, data_dir, "sid-1", source="musixmatch api", s3=fake_s3, cfg=cfg
    )
    assert asset_store.object_exists(cfg, fake_s3, artifact.content_hash)

    report = _purge(conn, data_dir, s3=fake_s3, cfg=cfg)

    assert report.r2_skipped_reason is None, "cloud mode actually visited R2"
    assert (report.objects_deleted, report.objects_absent) == (1, 0)
    assert not asset_store.object_exists(cfg, fake_s3, artifact.content_hash)
    assert (
        cfg.audio_bucket,
        asset_store.asset_object_key(artifact.content_hash),
    ) not in fake_s3.store


def test_counts_are_observed_per_store_never_inferred_from_each_other(
    conn: sqlite3.Connection,
    data_dir: Path,
    fake_s3: InMemoryAssetS3,
    cfg: CloudConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three rows that disagree about what is where, on purpose.

    ``sid-pinned`` has both a file and an object; ``sid-excluded`` recorded a
    hash but policy kept it local, so the object was never pushed;
    ``sid-fileless`` had its cache file removed out of band. A report that
    derived any count from ``rows_tombstoned`` would claim 3/3/3.
    """
    use_cloud_mode(monkeypatch)
    seed_stamped_policy(conn, asset_kind="karaoke_words", mode="pinned")
    pinned = _seed_verdict(
        conn, data_dir, "sid-pinned", source="musixmatch api", s3=fake_s3, cfg=cfg
    )
    fileless = _seed_verdict(
        conn, data_dir, "sid-fileless", source="musixmatch bulk", s3=fake_s3, cfg=cfg
    )
    fileless.path.unlink()
    conn.execute(
        "UPDATE sync_policies SET mode = 'excluded' WHERE asset_kind = 'karaoke_words'"
    )
    excluded = _seed_verdict(
        conn, data_dir, "sid-excluded", source="musixmatch web", s3=fake_s3, cfg=cfg
    )
    assert not asset_store.object_exists(cfg, fake_s3, excluded.content_hash)

    report = _purge(conn, data_dir, s3=fake_s3, cfg=cfg)

    assert report.rows_matched == report.rows_tombstoned == 3
    assert (report.files_removed, report.files_absent) == (2, 1)
    assert (report.objects_deleted, report.objects_absent) == (2, 1)
    assert not asset_store.object_exists(cfg, fake_s3, pinned.content_hash)
    assert not asset_store.object_exists(cfg, fake_s3, fileless.content_hash)


def test_a_row_with_no_words_hash_is_tombstoned_without_touching_r2(
    conn: sqlite3.Connection,
    data_dir: Path,
    fake_s3: InMemoryAssetS3,
    cfg: CloudConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No hash means no object key; a delete would be a guess."""
    use_cloud_mode(monkeypatch)
    seed_track(conn, "sid-1")
    store.upsert_verdict(
        conn,
        stable_id="sid-1",
        verdict="no-lyrics",
        coverage_pct=2.0,
        source="musixmatch api",
        language_iso3=None,
        n_words=None,
        n_lines=None,
        pct_witness_red=None,
        pipeline_version=PIPELINE_VERSION,
        words_content_hash=None,
        computed_at="2026-09-01T00:00:00.000000+00:00",
        resurrect=False,
    )
    report = _purge(conn, data_dir, s3=fake_s3, cfg=cfg)
    assert report.rows_tombstoned == 1
    assert (report.objects_deleted, report.objects_absent) == (0, 0)
    assert (report.files_removed, report.files_absent) == (0, 1)


def test_cloud_mode_without_an_s3_client_is_refused(
    conn: sqlite3.Connection, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A purge that cannot reach R2 must not report a partial success."""
    use_cloud_mode(monkeypatch)
    with pytest.raises(store.LyricStoreError, match="cannot reach R2"):
        _purge(conn, data_dir, s3=None, cfg=None)


def test_an_empty_prefix_is_refused(
    conn: sqlite3.Connection, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_local_mode(monkeypatch)
    with pytest.raises(store.LyricStoreError, match="whole library"):
        _purge(conn, data_dir, prefix="")


#-----------------------------------------------------------------------------
# dry run and idempotence
#-----------------------------------------------------------------------------
def test_dry_run_through_the_cli_writes_nothing(
    conn: sqlite3.Connection,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI passes (None, None) for the clients on a dry run, so the whole
    path is exercised without a credential in sight."""
    use_local_mode(monkeypatch)
    artifact = _seed_verdict(conn, data_dir, "sid-1", source="musixmatch api")
    before = _changelog_pks(conn)

    code = lyrics_cli(
        [
            "purge",
            "--source",
            "musixmatch",
            "--db-path",
            str(data_dir / "state" / "state.db"),
            "--dry-run",
        ]
    )

    assert code == 0
    printed = capsys.readouterr().out
    assert "[DRY-RUN]" in printed
    assert "1 live rows matched" in printed
    assert artifact.path.is_file(), "the rehearsal removed a file"
    assert store.get_verdict(conn, "sid-1") is not None, "the row is still live"
    assert _changelog_pks(conn) == before, "a dry run must log nothing"


def test_dry_run_in_cloud_mode_deletes_no_object(
    conn: sqlite3.Connection,
    data_dir: Path,
    fake_s3: InMemoryAssetS3,
    cfg: CloudConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It also refuses to claim an object count it did not observe."""
    use_cloud_mode(monkeypatch)
    seed_stamped_policy(conn, asset_kind="karaoke_words", mode="pinned")
    artifact = _seed_verdict(
        conn, data_dir, "sid-1", source="musixmatch api", s3=fake_s3, cfg=cfg
    )

    report = _purge(conn, data_dir, s3=fake_s3, cfg=cfg, dry_run=True)

    assert report.rows_matched == 1 and report.rows_tombstoned == 0
    assert report.files_removed == 1, "reports what it WOULD remove"
    assert artifact.path.is_file()
    assert asset_store.object_exists(cfg, fake_s3, artifact.content_hash)
    assert report.r2_skipped_reason == purge.DRY_RUN_R2_REASON
    assert (report.objects_deleted, report.objects_absent) == (0, 0)


def test_a_second_purge_over_the_same_prefix_tombstones_nothing(
    conn: sqlite3.Connection, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Idempotent: tombstoned rows are no longer live, so they no longer match."""
    use_local_mode(monkeypatch)
    _seed_verdict(conn, data_dir, "sid-1", source="musixmatch api")
    _seed_verdict(conn, data_dir, "sid-2", source="musixmatch bulk")
    first = _purge(conn, data_dir)
    logged = _changelog_pks(conn)

    second = _purge(conn, data_dir)

    assert first.rows_tombstoned == 2
    assert second.rows_matched == second.rows_tombstoned == 0
    assert second.stable_ids == ()
    assert (second.files_removed, second.files_absent) == (0, 0)
    assert _changelog_pks(conn) == logged, "nothing to push the second time"


def test_the_words_file_purged_is_the_one_the_cache_addresses(
    conn: sqlite3.Connection, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The purge unlinks by stable_id, exactly where the producer wrote."""
    use_local_mode(monkeypatch)
    artifact = _seed_verdict(conn, data_dir, "sid-1", source="musixmatch api")
    assert artifact.path == karaoke_cache.cache_path(data_dir, "sid-1")
    _purge(conn, data_dir)
    assert not karaoke_cache.cache_path(data_dir, "sid-1").exists()
