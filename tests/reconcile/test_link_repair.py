"""Synthetic tests for the state-layer link-repair matcher. Ties to RECON-01/02.

:class:`DiskAudio` entries are constructed in memory (plus a few real files
under ``tmp_path`` where existence matters), so tier ordering, the ambiguity
rule and the awaiting-volume rule are pinned exactly. The real-library half
(bucket-sum invariant, read-only guarantee against the actual state.db) lives
in ``test_link_repair_live.py``.

Failure lines are written so a red test reads as a statement about behaviour:
"if X then broken".
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apps.reconcile import index_disk, match
from apps.shared import paths


def _real_data_dir() -> Path:
    """Data dir holding the real library.

    A git worktree has its own empty ``data/``, so the real-library tests are
    pointed at the primary checkout with ``MDT_DATA_DIR=/path/to/data pytest``
    (the same env var :mod:`apps.shared.platform_paths` already honours). With
    the var unset this is the checkout's own ``data/`` and the tests skip
    cleanly when that is empty.
    """
    env = os.environ.get("MDT_DATA_DIR")
    return Path(env) if env else paths.DATA_DIR


DATA_DIR: Path = _real_data_dir()
STATE_DB: Path = DATA_DIR / "state" / "state.db"
RB_DB: Path = DATA_DIR / "master.plain.db"
INDEX_CACHE: Path = DATA_DIR / "state" / "disk-audio-index.json"


def _entry(
    path: str,
    *,
    size: int = 1000,
    mtime: float = 0.0,
    duration: float | None = None,
    title: str | None = None,
    artist: str | None = None,
    isrc: str | None = None,
) -> index_disk.DiskAudio:
    return index_disk.DiskAudio(
        path=path,
        size_bytes=size,
        mtime=mtime,
        tags_read=True,
        duration_s=duration,
        title=title,
        artist=artist,
        isrc=isrc,
    )


def _row(**over: object) -> match.TrackRow:
    base: dict[str, object] = {
        "stable_id": "row-1",
        "title": "Quiet Rooms",
        "artist": "Orla Finch, JUNO-K",
        "isrc": None,
        "duration_ms": 232000,
        "file_path": "/Users/old/Music/Convert/02 Quiet Rooms.mp3",
    }
    base.update(over)
    return match.TrackRow(**base)  # type: ignore[arg-type]


# ----- normalisation -----------------------------------------------------


@pytest.mark.requirement("RECON-02")
def test_normalisation_strips_tracknos_brackets_and_punctuation() -> None:
    assert index_disk.normalise_text("01 - Quiet Rooms (feat. JUNO-K)") == "quiet rooms"
    assert index_disk.normalise_text("6. Quiet  Rooms!") == "quiet rooms"
    assert index_disk.normalise_text("Quiet Rooms [Extended Mix]") == "quiet rooms"
    assert index_disk.normalise_text("Tom & Jerry") == "tom and jerry"
    assert index_disk.normalise_text("Beyoncé") == "beyonce"
    assert index_disk.normalise_text(None) == "", (
        "if normalise_text(None) is not '' then callers cannot test truthiness"
    )


@pytest.mark.requirement("RECON-02")
def test_isrc_normalisation_rejects_non_twelve_char_values() -> None:
    assert index_disk.normalise_isrc("gb-ayE-21-01234") == "GBAYE2101234"
    assert index_disk.normalise_isrc("nope") is None, (
        "if a 4-char string passes as an ISRC then the strongest tag tier "
        "fires on garbage"
    )
    assert index_disk.normalise_isrc(None) is None


# ----- tier ordering ----------------------------------------------------


@pytest.mark.requirement("RECON-02")
def test_tier_a_fires_only_when_basename_is_unique() -> None:
    index = index_disk.DiskIndex.build([_entry("/disk/a/02 Quiet Rooms.mp3")])
    cands = match.find_candidates(_row(), index)
    assert [c.tier for c in cands] == ["basename-exact-unique"]
    assert cands[0].confidence == match.CONF_BASENAME_UNIQUE
    assert cands[0].auto_applicable


@pytest.mark.requirement("RECON-02")
def test_duplicate_basename_falls_through_to_duration_tier() -> None:
    """Tier a must NOT fire on a shared basename; tier b disambiguates."""
    index = index_disk.DiskIndex.build(
        [
            _entry("/disk/a/02 Quiet Rooms.mp3", duration=232.0, size=111),
            _entry("/disk/b/02 Quiet Rooms.mp3", duration=95.0, size=222),
        ]
    )
    cands = match.find_candidates(_row(), index)
    assert [c.tier for c in cands] == ["basename-duration"], (
        "if a shared basename still reports basename-exact-unique then the "
        "ordering is broken and ambiguity is being hidden"
    )
    assert [c.path for c in cands] == ["/disk/a/02 Quiet Rooms.mp3"]


@pytest.mark.requirement("RECON-02")
def test_size_tier_fires_only_when_duration_tier_cannot() -> None:
    index = index_disk.DiskIndex.build(
        [
            _entry("/disk/a/02 Quiet Rooms.mp3", duration=None, size=5000),
            _entry("/disk/b/02 Quiet Rooms.mp3", duration=None, size=9999),
        ]
    )
    cands = match.find_candidates(_row(expected_size_bytes=5000), index)
    assert [c.tier for c in cands] == ["basename-size"]
    assert cands[0].path == "/disk/a/02 Quiet Rooms.mp3"


@pytest.mark.requirement("RECON-02")
def test_isrc_tier_fires_when_no_basename_tier_can() -> None:
    index = index_disk.DiskIndex.build(
        [_entry("/disk/renamed.mp3", isrc="GBAYE2101234", duration=232.0)]
    )
    cands = match.find_candidates(_row(isrc="GBAYE2101234"), index)
    assert [c.tier for c in cands] == ["isrc"]
    assert cands[0].confidence == match.CONF_ISRC


@pytest.mark.requirement("RECON-02")
def test_basename_tier_outranks_isrc_per_specified_order() -> None:
    """A unique basename hit stops evaluation before the ISRC tier is asked."""
    index = index_disk.DiskIndex.build(
        [
            _entry("/disk/a/02 Quiet Rooms.mp3"),
            _entry("/disk/elsewhere.mp3", isrc="GBAYE2101234"),
        ]
    )
    cands = match.find_candidates(_row(isrc="GBAYE2101234"), index)
    assert [c.tier for c in cands] == ["basename-exact-unique"]


@pytest.mark.requirement("RECON-02")
def test_title_artist_fuzzy_tier_stays_below_auto_apply_threshold() -> None:
    index = index_disk.DiskIndex.build(
        [
            _entry(
                "/disk/whatever-name.mp3",
                title="Quiet Rooms (feat. JUNO-K)",
                artist="Orla Finch, JUNO-K",
                duration=900.0,
            )
        ]
    )
    cands = match.find_candidates(_row(), index)
    assert [c.tier for c in cands] == ["title-artist-fuzzy"]
    assert not cands[0].auto_applicable, (
        "if a fuzzy text match is auto-applicable then the library can be "
        "repointed on text similarity alone"
    )


@pytest.mark.requirement("RECON-02")
def test_duration_title_fuzzy_tier_is_the_last_resort() -> None:
    index = index_disk.DiskIndex.build(
        [_entry("/disk/Quiet Rooms.flac", duration=232.4, title=None, artist=None)]
    )
    cands = match.find_candidates(_row(), index)
    assert [c.tier for c in cands] == ["duration-title-fuzzy"]
    assert "filename similarity" in cands[0].reason
    assert not cands[0].auto_applicable


@pytest.mark.requirement("RECON-02")
def test_no_candidate_yields_empty_list() -> None:
    index = index_disk.DiskIndex.build([_entry("/disk/unrelated-noise.wav")])
    assert match.find_candidates(_row(), index) == []


# ----- ambiguity rule ---------------------------------------------------


@pytest.mark.requirement("RECON-02")
def test_two_candidates_above_threshold_are_ambiguous_never_auto() -> None:
    index = index_disk.DiskIndex.build(
        [
            _entry("/disk/a/02 Quiet Rooms.mp3", duration=232.0),
            _entry("/disk/b/02 Quiet Rooms.mp3", duration=232.5),
        ]
    )
    res = match.classify_row(_row(), index, frozenset({"Macintosh HD"}))
    assert res.bucket == "relinkable-ambiguous"
    assert res.ambiguity == "multiple-above-threshold"
    assert len(res.candidates) == 2
    plan = match.build_plan([res])
    assert plan["entries"] == [], (
        "if an ambiguous row appears in the auto plan then 2+ strong "
        "candidates can be silently applied"
    )


@pytest.mark.requirement("RECON-02")
def test_single_below_threshold_candidate_is_ambiguous_not_auto() -> None:
    index = index_disk.DiskIndex.build(
        [
            _entry(
                "/disk/renamed.mp3",
                title="Quiet Rooms",
                artist="Orla Finch, JUNO-K",
                duration=900.0,
            )
        ]
    )
    res = match.classify_row(_row(), index, frozenset({"Macintosh HD"}))
    assert res.bucket == "relinkable-ambiguous"
    assert res.ambiguity == "below-auto-threshold"


@pytest.mark.requirement("RECON-02")
def test_single_strong_candidate_is_auto_and_plan_is_reversible() -> None:
    index = index_disk.DiskIndex.build([_entry("/disk/a/02 Quiet Rooms.mp3")])
    res = match.classify_row(_row(), index, frozenset({"Macintosh HD"}))
    assert res.bucket == "relinkable-auto"
    entry = match.build_plan([res])["entries"][0]  # type: ignore[index]
    assert entry["old_path"] == _row().file_path
    assert entry["new_path"] == "/disk/a/02 Quiet Rooms.mp3"
    assert entry["tier"] == "basename-exact-unique", (
        "if the plan entry loses the tier then a reversal cannot be audited"
    )


@pytest.mark.requirement("RECON-02")
def test_candidate_already_linked_to_another_row_is_not_auto(tmp_path: Path) -> None:
    taken = tmp_path / "02 Quiet Rooms.mp3"
    taken.write_bytes(b"x")
    index = index_disk.DiskIndex.build([_entry(str(taken))])
    res = match.classify_row(
        _row(),
        index,
        frozenset({"Macintosh HD"}),
        linked_paths=frozenset({str(taken)}),
    )
    assert res.bucket == "relinkable-ambiguous"
    assert res.ambiguity == "target-already-linked", (
        "if a file that already resolves for another row can be auto-applied "
        "then a repair silently points two rows at one file"
    )


@pytest.mark.requirement("RECON-02")
def test_two_auto_rows_contesting_one_file_are_both_demoted(tmp_path: Path) -> None:
    target = tmp_path / "dup-name.mp3"
    target.write_bytes(b"x")
    index = index_disk.DiskIndex.build([_entry(str(target))])
    rows = [
        _row(stable_id="a", file_path="/gone/one/dup-name.mp3"),
        _row(stable_id="b", file_path="/gone/two/dup-name.mp3"),
    ]
    results = match.classify_rows(rows, index, mounted=frozenset({"Macintosh HD"}))
    assert [r.bucket for r in results] == ["relinkable-ambiguous"] * 2
    assert {r.ambiguity for r in results} == {"target-contested"}
    assert match.build_plan(results)["entries"] == [], (
        "if two rows both auto-apply to one file then the repair fans several "
        "library rows onto a single audio file"
    )


@pytest.mark.requirement("RECON-02")
def test_uncontested_auto_row_survives_the_demotion_pass(tmp_path: Path) -> None:
    target = tmp_path / "solo-name.mp3"
    target.write_bytes(b"x")
    index = index_disk.DiskIndex.build([_entry(str(target))])
    results = match.classify_rows(
        [_row(stable_id="a", file_path="/gone/one/solo-name.mp3")],
        index,
        mounted=frozenset({"Macintosh HD"}),
    )
    assert [r.bucket for r in results] == ["relinkable-auto"]
    assert match.demote_contested_targets(results) == 0


# ----- awaiting-volume --------------------------------------------------


@pytest.mark.requirement("RECON-01")
def test_unmounted_volume_row_is_awaiting_volume_and_never_matched() -> None:
    index = index_disk.DiskIndex.build([_entry("/disk/a/track.mp3")])
    row = _row(file_path="/Volumes/SLATER/dj/track.mp3")
    res = match.classify_row(row, index, frozenset({"Macintosh HD"}))
    assert res.bucket == "awaiting-volume"
    assert res.candidates == [], (
        "if an offline-volume row carries candidates then a relink can be "
        "proposed for a file that is probably fine on an unplugged drive"
    )


@pytest.mark.requirement("RECON-01")
def test_mounted_volume_row_with_missing_file_is_matched_normally() -> None:
    index = index_disk.DiskIndex.build([_entry("/disk/a/track.mp3")])
    row = _row(file_path="/Volumes/SLATER/dj/track.mp3")
    res = match.classify_row(row, index, frozenset({"Macintosh HD", "SLATER"}))
    assert res.bucket == "relinkable-auto", (
        "if a MOUNTED volume's missing file is still awaiting-volume then real "
        "dead paths on a plugged-in drive never get repaired"
    )


@pytest.mark.requirement("RECON-01")
def test_mounted_volume_names_reflects_runtime_state() -> None:
    names = match.mounted_volume_names()
    assert isinstance(names, frozenset)
    if Path("/Volumes").is_dir():
        assert names, "if /Volumes exists but reports no volumes then detection is broken"


# ----- other buckets ---------------------------------------------------


@pytest.mark.requirement("RECON-01")
def test_present_bucket_uses_real_disk_existence(tmp_path: Path) -> None:
    real = tmp_path / "here.mp3"
    real.write_bytes(b"x")
    index = index_disk.DiskIndex.build([])
    res = match.classify_row(
        _row(file_path=str(real)), index, frozenset({"Macintosh HD"})
    )
    assert res.bucket == "present"


@pytest.mark.requirement("RECON-01")
@pytest.mark.parametrize(
    "path",
    [None, "", "   ", "relative/path.mp3", "/contents_815473895/x/y.mp3"],
)
def test_malformed_paths_are_bucketed_malformed(path: str | None) -> None:
    index = index_disk.DiskIndex.build([])
    res = match.classify_row(
        _row(file_path=path), index, frozenset({"Macintosh HD"})
    )
    assert res.bucket == "malformed-path"


@pytest.mark.requirement("RECON-01")
@pytest.mark.parametrize(
    "uri",
    [
        "spotify:track:07chlhnQOZfhWzIH1068re",
        "soundcloud:tracks:1104342268",
        "tidal:track:12345",
    ],
)
def test_streaming_uris_are_not_malformed(uri: str) -> None:
    index = index_disk.DiskIndex.build([])
    res = match.classify_row(_row(file_path=uri), index, frozenset({"Macintosh HD"}))
    assert res.bucket == "streaming", (
        "if a spotify/soundcloud URI is called a malformed path then the "
        "report asserts something false about 387 real rows"
    )


@pytest.mark.requirement("RECON-01")
def test_absent_no_audio_when_nothing_on_disk_resembles_the_row() -> None:
    index = index_disk.DiskIndex.build([_entry("/disk/completely-other.wav")])
    res = match.classify_row(
        _row(file_path="/Users/old/Music/gone/xyzzy-9x8.mp3", title="Xyzzy 9x8"),
        index,
        frozenset({"Macintosh HD"}),
    )
    assert res.bucket == "absent-no-audio"


@pytest.mark.requirement("RECON-01")
def test_buckets_are_mutually_exclusive_and_sum_on_synthetic_rows(
    tmp_path: Path,
) -> None:
    real = tmp_path / "present.mp3"
    real.write_bytes(b"x")
    index = index_disk.DiskIndex.build(
        [
            _entry("/disk/a/02 Quiet Rooms.mp3"),
            _entry("/disk/b/dup.mp3", duration=100.0),
            _entry("/disk/c/dup.mp3", duration=100.4),
        ]
    )
    rows = [
        _row(stable_id="present", file_path=str(real)),
        _row(stable_id="auto"),
        _row(stable_id="amb", file_path="/gone/dup.mp3", duration_ms=100000),
        _row(stable_id="vol", file_path="/Volumes/SLATER/x.mp3"),
        _row(stable_id="bad", file_path=None),
        _row(stable_id="stream", file_path="spotify:track:abc"),
        _row(stable_id="absent", file_path="/gone/qqzz-77.mp3", title="Qqzz 77"),
    ]
    results = match.classify_rows(rows, index, mounted=frozenset({"Macintosh HD"}))
    counts = match.bucket_counts(results)
    assert sum(counts.values()) == len(rows)
    assert counts["present"] == 1
    assert counts["relinkable-auto"] == 1
    assert counts["relinkable-ambiguous"] == 1
    assert counts["awaiting-volume"] == 1
    assert counts["malformed-path"] == 1
    assert counts["streaming"] == 1
    assert counts["absent-no-audio"] == 1
    assert len({r.stable_id for r in results}) == len(rows), (
        "if a row is classified twice then the buckets are not exclusive"
    )


@pytest.mark.requirement("RECON-01")
def test_classify_rows_raises_when_a_bucket_is_unknown(monkeypatch) -> None:
    """The sum invariant must be an error, not a plausible-looking report."""
    index = index_disk.DiskIndex.build([])
    bogus = match.RowResult(
        stable_id="x", file_path=None, bucket="not-a-bucket", reason=""  # type: ignore[arg-type]
    )
    with pytest.raises(AssertionError):
        match.bucket_counts([bogus])
    monkeypatch.setattr(match, "classify_row", lambda *a, **k: bogus)
    with pytest.raises(AssertionError):
        match.classify_rows([_row()], index, mounted=frozenset())


# ----- disk index cache -------------------------------------------------


@pytest.mark.requirement("RECON-02")
def test_index_cache_reuses_unchanged_files_and_rereads_changed_ones(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Music"
    root.mkdir()
    (root / "one.mp3").write_bytes(b"a" * 32)
    (root / "two.wav").write_bytes(b"b" * 32)
    cache = tmp_path / "index.json"

    _, first = index_disk.build_index([root], cache_path=cache)
    assert first.walked == 2 and first.tag_reads == 2 and first.reused == 0

    _, second = index_disk.build_index([root], cache_path=cache)
    assert second.reused == 2 and second.tag_reads == 0, (
        "if an unchanged file is re-tagged then a rescan costs a full "
        "7k-file tag-read pass every time"
    )

    (root / "one.mp3").write_bytes(b"a" * 64)
    _, third = index_disk.build_index([root], cache_path=cache)
    assert third.tag_reads == 1 and third.reused == 1

    (root / "two.wav").unlink()
    _, fourth = index_disk.build_index([root], cache_path=cache)
    assert fourth.walked == 1 and fourth.dropped == 1


@pytest.mark.requirement("RECON-02")
def test_index_skips_dot_dirs_node_modules_and_non_audio(tmp_path: Path) -> None:
    root = tmp_path / "Music"
    (root / ".hidden").mkdir(parents=True)
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / ".hidden" / "x.mp3").write_bytes(b"x")
    (root / "node_modules" / "pkg" / "y.mp3").write_bytes(b"y")
    (root / "notes.txt").write_bytes(b"z")
    (root / "keep.mp3").write_bytes(b"k")
    index, stats = index_disk.build_index(
        [root], cache_path=tmp_path / "c.json", write_cache=False
    )
    assert stats.walked == 1
    assert [Path(e.path).name for e in index.entries] == ["keep.mp3"]


@pytest.mark.requirement("RECON-02")
def test_dataless_files_are_indexed_but_never_opened(
    tmp_path: Path, monkeypatch
) -> None:
    """An iCloud placeholder must not be read: that would fault it down.

    We cannot set SF_DATALESS in a test, so ``is_dataless`` is forced true and
    ``read_tags`` is booby-trapped -- if the walker opens the file the test
    fails loudly instead of quietly costing a download.
    """
    root = tmp_path / "Documents"
    root.mkdir()
    (root / "offloaded.mp3").write_bytes(b"a" * 16)

    def _boom(path: Path) -> index_disk.TagRead:
        raise AssertionError(
            "if read_tags runs on a dataless file then an index build silently "
            "downloads the whole iCloud library"
        )

    monkeypatch.setattr(index_disk, "is_dataless", lambda st: True)
    monkeypatch.setattr(index_disk, "read_tags", _boom)
    index, stats = index_disk.build_index(
        [root], cache_path=tmp_path / "c.json", write_cache=False
    )
    assert stats.dataless_skipped == 1 and stats.tag_reads == 0
    entry = index.entries[0]
    assert entry.dataless and not entry.tags_read
    assert entry.size_bytes == 16, (
        "if a dataless entry loses its size then the basename-size tier cannot "
        "recover offloaded files"
    )


@pytest.mark.requirement("RECON-02")
def test_index_missing_root_is_skipped_not_an_error(tmp_path: Path) -> None:
    index, stats = index_disk.build_index(
        [tmp_path / "nope"], cache_path=tmp_path / "c.json", write_cache=False
    )
    assert stats.walked == 0 and index.entries == []


@pytest.mark.requirement("RECON-02")
def test_corrupt_cache_is_rebuilt_rather_than_raising(tmp_path: Path) -> None:
    root = tmp_path / "Music"
    root.mkdir()
    (root / "one.mp3").write_bytes(b"a")
    cache = tmp_path / "index.json"
    cache.write_text("{not json", encoding="utf-8")
    _, stats = index_disk.build_index([root], cache_path=cache)
    assert stats.tag_reads == 1
    written = json.loads(cache.read_text(encoding="utf-8"))
    assert written["version"] == index_disk.CACHE_VERSION
