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
from pathlib import Path

import pytest

from apps.cloud.config import CloudConfig
from apps.cloud.lock import FakeS3Client
from apps.cloud.stem_index import (
    INDEX_OBJECT_KEY,
    StemIndexError,
    build_index_from_journal,
    fetch_index,
    load_cached_index,
    publish_index,
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
    assert build_index_from_journal(tmp_path / "nope.jsonl") == {}


@pytest.mark.requirement("STEM-09")
def test_build_index_later_line_wins_for_rerendered_bundle(tmp_path: Path):
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
    journal = tmp_path / "journal.jsonl"
    good = _journal_line(stable_id="t", files={"vocals.mp3": "1" * 64})
    journal.write_text(good + "\n" + '{"at": "2026", "obje', encoding="utf-8")
    index = build_index_from_journal(journal)
    assert index == {"t": {"vocals.mp3": "1" * 64}}


@pytest.mark.requirement("STEM-09")
def test_build_index_raises_on_corrupt_earlier_line(tmp_path: Path):
    journal = tmp_path / "journal.jsonl"
    good = _journal_line(stable_id="t", files={"vocals.mp3": "1" * 64})
    journal.write_text("not json at all\n" + good + "\n", encoding="utf-8")
    with pytest.raises(StemIndexError):
        build_index_from_journal(journal)


@pytest.mark.requirement("STEM-09")
def test_build_index_ignores_non_stem_journal_entries(tmp_path: Path):
    """A journal line naming an object outside the legacy stems/ layout (a
    different rail sharing the shape one day) is skipped, not fatal."""
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
    the second publish must REPLACE the body, not treat 'exists' as done."""
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
    s3 = FakeS3Client()
    assert fetch_index(cfg, s3) == {}


@pytest.mark.requirement("STEM-10")
def test_fetch_index_round_trips_publish(cfg: CloudConfig):
    s3 = FakeS3Client()
    index = {"track-a": {"vocals.mp3": "f" * 64}}
    publish_index(cfg, s3, index)
    assert fetch_index(cfg, s3) == index


@pytest.mark.requirement("STEM-11")
def test_local_cache_round_trips(tmp_path: Path):
    data_dir = tmp_path / "data"
    index = {"track-a": {"manifest.json": "a" * 64}}
    save_cached_index(data_dir, index)
    assert load_cached_index(data_dir) == index


@pytest.mark.requirement("STEM-11")
def test_local_cache_missing_is_empty_not_error(tmp_path: Path):
    assert load_cached_index(tmp_path / "data") == {}
