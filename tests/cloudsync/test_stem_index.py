"""Stem bundle hydration index (ADR-0024): journal -> index -> R2 pointer.

* [if] the journal has (legacy_key, key) pairs for a stable_id's manifest and
  parts [then] the index maps that stable_id's filenames to their sha256.
* [if] a bundle is re-rendered and journaled again with a new sha256 for the
  same filename [then] the LATER journal line wins.
* [if] the journal's final line is torn [then] the index still builds from
  every earlier well-formed line; [if] an EARLIER line is corrupt [then]
  building raises rather than silently skipping it.
* [if] the R2 index object does not exist yet [then] publishing creates it;
  [if] it already exists [then] publishing replaces its body (overwrite, not
  content-addressed create).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from apps.cloud.config import CloudConfig
from apps.cloud.lock import FakeS3Client
from apps.cloud.stem_index import (
    INDEX_OBJECT_KEY,
    MANIFEST_FILENAME,
    StemIndexError,
    build_index_from_journal,
    fetch_index,
    is_allowed_stem_filename,
    load_cached_index,
    publish_index,
    refresh_error,
    refresh_local_cache_from_r2_throttled,
    save_cached_index,
)


def _journal_line(*, stable_id: str, preset: str = "4.0.1", files: dict[str, str]) -> str:
    """One journal record naming (legacy_key, key) for each file -> hash."""
    objects = [
        {
            "legacy_key": f"stems/{preset}/{stable_id}/{filename}",
            "key": f"assets/{digest[:2]}/{digest}",
        }
        for filename, digest in files.items()
    ]
    return json.dumps({"at": "2026-09-14T00:00:00Z", "objects": objects})


@pytest.mark.requirement("STEM-09")
def test_build_index_from_journal_maps_stable_id_to_file_hashes(tmp_path: Path):
    """[if] a journal line lists file hashes [then] the index maps filename to hash, [else stop]."""
    journal = tmp_path / "journal.jsonl"
    journal.write_text(
        _journal_line(
            stable_id="track-a",
            files={
                "manifest.json": "a" * 64,
                "vocals.mp3": "b" * 64,
                "drums.mp3": "c" * 64,
                "bass.mp3": "d" * 64,
                "other.mp3": "e" * 64,
            },
        )
        + "\n",
        encoding="utf-8",
    )
    index = build_index_from_journal(journal)
    assert index == {
        "track-a": {
            "manifest.json": "a" * 64,
            "vocals.mp3": "b" * 64,
            "drums.mp3": "c" * 64,
            "bass.mp3": "d" * 64,
            "other.mp3": "e" * 64,
        }
    }


@pytest.mark.requirement("STEM-09")
def test_build_index_missing_journal_is_empty_not_error(tmp_path: Path):
    """[if] the journal file is missing [then] build_index_from_journal is empty, [else stop]."""
    assert build_index_from_journal(tmp_path / "nope.jsonl") == {}


@pytest.mark.requirement("STEM-09")
def test_build_index_later_line_wins_for_rerendered_bundle(tmp_path: Path):
    """[if] the journal has two lines, same id [then] the later hash wins, [else stop]."""
    journal = tmp_path / "journal.jsonl"
    journal.write_text(
        _journal_line(stable_id="t", files={"vocals.mp3": "1" * 64}) + "\n"
        + _journal_line(stable_id="t", files={"vocals.mp3": "2" * 64}) + "\n",
        encoding="utf-8",
    )
    index = build_index_from_journal(journal)
    assert index["t"]["vocals.mp3"] == "2" * 64


@pytest.mark.requirement("STEM-09")
def test_build_index_tolerates_torn_final_line(tmp_path: Path):
    """[if] the journal's final line is torn [then] the index keeps prior lines, [else stop]."""
    journal = tmp_path / "journal.jsonl"
    good = _journal_line(stable_id="t", files={"vocals.mp3": "1" * 64})
    journal.write_text(good + "\n" + '{"at": "2026", "obje', encoding="utf-8")
    index = build_index_from_journal(journal)
    assert index == {"t": {"vocals.mp3": "1" * 64}}


@pytest.mark.requirement("STEM-09")
def test_build_index_raises_on_corrupt_earlier_line(tmp_path: Path):
    """[if] a non-final journal line is bad JSON [then] build_index raises, [else stop]."""
    journal = tmp_path / "journal.jsonl"
    good = _journal_line(stable_id="t", files={"vocals.mp3": "1" * 64})
    journal.write_text("not json at all\n" + good + "\n", encoding="utf-8")
    with pytest.raises(StemIndexError):
        build_index_from_journal(journal)


@pytest.mark.requirement("STEM-09")
def test_build_index_ignores_non_stem_journal_entries(tmp_path: Path):
    """A journal line naming an object outside the legacy stems/ layout (a
    different rail sharing the shape one day) is skipped, not fatal.

    [if] a journal entry's legacy_key is outside stems/ [then] it is skipped, [else stop].
    """
    journal = tmp_path / "journal.jsonl"
    record = {
        "at": "2026",
        "objects": [{"legacy_key": "audio/somewhere/file.mp3", "key": "assets/aa/" + "a" * 64}],
    }
    journal.write_text(json.dumps(record) + "\n", encoding="utf-8")
    assert build_index_from_journal(journal) == {}


@pytest.mark.requirement("STEM-10")
def test_publish_index_creates_then_overwrites(cfg: CloudConfig):
    """The one deliberate non-content-addressed R2 write in this codebase:
    the second publish must REPLACE the body, not treat 'exists' as done.

    [if] publish_index runs twice, different indexes [then] second overwrites, new etag, [else stop]
    """
    s3 = FakeS3Client()
    etag1 = publish_index(cfg, s3, {"a": {"manifest.json": "1" * 64}})
    got1 = s3.get_object(cfg.audio_bucket, INDEX_OBJECT_KEY)
    assert got1 is not None
    body1, _ = got1
    assert json.loads(body1)["stable_ids"] == {"a": {"manifest.json": "1" * 64}}

    etag2 = publish_index(
        cfg, s3, {"a": {"manifest.json": "1" * 64}, "b": {"manifest.json": "2" * 64}}
    )
    got2 = s3.get_object(cfg.audio_bucket, INDEX_OBJECT_KEY)
    assert got2 is not None
    body2, _ = got2
    assert json.loads(body2)["stable_ids"]["b"]["manifest.json"] == "2" * 64
    assert etag1 != etag2  # a real overwrite happened, not a silent no-op


@pytest.mark.requirement("STEM-10")
def test_fetch_index_absent_object_is_empty(cfg: CloudConfig):
    """[if] no index has been published [then] fetch_index returns empty, [else stop]."""
    s3 = FakeS3Client()
    assert fetch_index(cfg, s3) == {}


@pytest.mark.requirement("STEM-10")
def test_fetch_index_round_trips_publish(cfg: CloudConfig):
    """[if] an index is published then fetched [then] fetch_index returns it, [else stop]."""
    s3 = FakeS3Client()
    index = {"track-a": {"vocals.mp3": "f" * 64}}
    publish_index(cfg, s3, index)
    assert fetch_index(cfg, s3) == index


@pytest.mark.requirement("STEM-11")
def test_local_cache_round_trips(tmp_path: Path):
    """[if] an index is cached then loaded [then] load_cached_index returns it, [else stop]."""
    data_dir = tmp_path / "data"
    index = {"track-a": {"manifest.json": "a" * 64}}
    save_cached_index(data_dir, index)
    assert load_cached_index(data_dir) == index


@pytest.mark.requirement("STEM-11")
def test_local_cache_missing_is_empty_not_error(tmp_path: Path):
    """[if] no local cache file exists [then] load_cached_index returns empty, [else stop]."""
    assert load_cached_index(tmp_path / "data") == {}


@pytest.mark.requirement("STEM-19")
def test_local_cache_corrupt_json_raises(tmp_path: Path):
    """[if] the cache file is bad JSON [then] load_cached_index raises naming it, [else stop]."""
    data_dir = tmp_path / "data"
    path = tmp_path / "data" / "state" / "stem-bundle-index.json"
    path.parent.mkdir(parents=True)
    path.write_text("not json at all", encoding="utf-8")
    with pytest.raises(StemIndexError, match=re.escape("stem-bundle-index.json")):
        load_cached_index(data_dir)


@pytest.mark.requirement("STEM-19")
def test_local_cache_missing_stable_ids_key_raises(tmp_path: Path):
    """[if] the cache lacks stable_ids [then] load_cached_index raises naming it, [else stop]."""
    data_dir = tmp_path / "data"
    path = tmp_path / "data" / "state" / "stem-bundle-index.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    with pytest.raises(StemIndexError, match=re.escape("stem-bundle-index.json")):
        load_cached_index(data_dir)


@pytest.mark.requirement("STEM-22")
def test_is_allowed_stem_filename_accepts_real_bundle_basenames():
    """Over-strict allowlist guard: every real demucs/roformer basename must pass.

    [if] a name is a real bundle basename [then] is_allowed_stem_filename accepts it, [else stop]
    """
    positives = [
        MANIFEST_FILENAME,
        "vocals.wav",
        "vocals.flac",
        "vocals.mp3",
        "drums.wav",
        "bass.wav",
        "other.wav",
        "instrumental.wav",
        "instrumental.flac",
        "instrumental.mp3",
    ]
    for filename in positives:
        assert is_allowed_stem_filename(filename)


@pytest.mark.requirement("STEM-22")
def test_is_allowed_stem_filename_rejects_traversal_and_unknown_names():
    """[if] traversal/unknown ext [then] is_allowed_stem_filename rejects it, [else stop]"""
    negatives = [
        "../x",
        "/etc/passwd",
        "a/b/vocals.wav",
        "vocals.exe",
        "",
        "bonus.wav",
    ]
    for filename in negatives:
        assert not is_allowed_stem_filename(filename)


@pytest.mark.requirement("STEM-22")
def test_local_cache_disallowed_filename_raises(tmp_path: Path):
    """[if] a cached index names a traversal filename [then] load raises naming it, [else stop]."""
    data_dir = tmp_path / "data"
    path = tmp_path / "data" / "state" / "stem-bundle-index.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "stable_ids": {
                    "evil-track": {
                        "../evil": "a" * 64,
                        "manifest.json": "b" * 64,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(StemIndexError, match=re.escape("../evil")):
        load_cached_index(data_dir)


@pytest.mark.requirement("STEM-23")
def test_publish_index_merges_concurrent_updates_under_cas(cfg: CloudConfig):
    """[if] publisher B lands a write strictly between publisher A's read and
    A's write [then] A's first CAS write hits a real precondition failure,
    A retries against the fresh state, and both publishers' entries survive.

    Deterministic, not thread/timing-dependent: ``get_object`` returns the
    snapshot captured BEFORE publisher B's write (so A's first attempt is
    provably stale), then triggers B's publish as a side effect. This forces
    A's ``put_object_if_none_match`` to fail against the real store (B
    already created the object), proving the retry loop -- not just the
    merge -- actually ran. A mutation back to the old blind-overwrite
    ``upsert_object`` fails this test two ways: it never reads first (so B's
    nested publish is never triggered by this hook), and even if it were, it
    would overwrite B's entry outright instead of merging it.

    [if] a second publisher writes mid-read [then] the CAS retry merges, no overwrite, [else stop].
    """
    b_index = {"b": {"manifest.json": "b" * 64}}
    a_index = {"a": {"manifest.json": "a" * 64}}

    class InterleavingS3(FakeS3Client):
        def __init__(self) -> None:
            super().__init__()
            self.get_calls = 0

        def get_object(self, bucket: str, key: str):
            self.get_calls += 1
            stale_snapshot = super().get_object(bucket, key)
            if key == INDEX_OBJECT_KEY and self.get_calls == 1:
                # B publishes AFTER we captured A's snapshot, so A's
                # upcoming write is provably against state that has moved.
                publish_index(cfg, self, b_index)
            return stale_snapshot

    s3 = InterleavingS3()
    publish_index(cfg, s3, a_index)
    # 1 (A's stale read) + 1 (B's nested read) + 1 (A's retry re-read) = 3.
    # Fewer than 3 would mean A's write landed without ever re-reading after
    # B's write, i.e. no real retry happened.
    assert s3.get_calls >= 3
    final = fetch_index(cfg, s3)
    assert final == {"a": {"manifest.json": "a" * 64}, "b": {"manifest.json": "b" * 64}}


@pytest.fixture(autouse=True)
def _reset_stem_index_refresh_state():
    from apps.cloud import stem_index

    stem_index._last_refresh_attempt_mono.clear()
    stem_index._last_refresh_error.clear()
    yield
    stem_index._last_refresh_attempt_mono.clear()
    stem_index._last_refresh_error.clear()


class _CountingGetS3(FakeS3Client):
    def __init__(self) -> None:
        super().__init__()
        self.get_calls = 0

    def get_object(self, bucket: str, key: str):
        self.get_calls += 1
        return super().get_object(bucket, key)


@pytest.mark.requirement("STEM-26")
def test_refresh_local_cache_from_r2_throttled_runs_once_within_interval(
    tmp_path: Path, cfg: CloudConfig
):
    """[if] refresh runs twice inside backoff [then] the second call is a no-op, [else stop]."""
    data_dir = tmp_path / "data"
    s3 = _CountingGetS3()
    publish_index(cfg, s3, {"t": {"manifest.json": "a" * 64}})
    s3.get_calls = 0

    assert refresh_local_cache_from_r2_throttled(cfg, s3, data_dir) is True
    assert refresh_local_cache_from_r2_throttled(cfg, s3, data_dir) is False
    assert s3.get_calls == 1


@pytest.mark.requirement("STEM-26")
def test_refresh_local_cache_from_r2_throttled_force_bypasses_backoff(
    tmp_path: Path, cfg: CloudConfig
):
    """[if] force=True during backoff [then] refresh still makes a fresh R2 request, [else stop]."""
    data_dir = tmp_path / "data"
    s3 = _CountingGetS3()
    publish_index(cfg, s3, {"t": {"manifest.json": "a" * 64}})
    s3.get_calls = 0

    refresh_local_cache_from_r2_throttled(cfg, s3, data_dir)
    assert refresh_local_cache_from_r2_throttled(cfg, s3, data_dir) is False
    assert refresh_local_cache_from_r2_throttled(cfg, s3, data_dir, force=True) is True
    assert s3.get_calls == 2


@pytest.mark.requirement("STEM-26")
def test_refresh_local_cache_from_r2_throttled_records_and_clears_error(
    tmp_path: Path, cfg: CloudConfig
):
    """[if] a refresh fails then later succeeds [then] refresh_error sets, clears, [else stop]."""
    data_dir = tmp_path / "data"

    class FailingS3(FakeS3Client):
        def get_object(self, bucket: str, key: str):
            raise TimeoutError("simulated failure")

    failing = FailingS3()
    with pytest.raises(TimeoutError):
        refresh_local_cache_from_r2_throttled(cfg, failing, data_dir, force=True)
    assert refresh_error(data_dir) is not None

    s3 = FakeS3Client()
    publish_index(cfg, s3, {"t": {"manifest.json": "a" * 64}})
    refresh_local_cache_from_r2_throttled(cfg, s3, data_dir, force=True)
    assert refresh_error(data_dir) is None
