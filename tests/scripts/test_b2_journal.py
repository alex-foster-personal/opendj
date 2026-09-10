"""Contract tests for scripts/b2_journal.py's pure journal bookkeeping.

Split out alongside the module it tests (see that module's docstring for
why): the journal-mapping and swap-detection decisions the bifrost2 -> R2
backfill makes before touching the network, pinned here without any client.

  [if] the journal only records jobs uploaded THIS run, an already-present
       object or one whose sibling PUT failed never gets its mapping
       recorded -> test_new_journal_mappings_includes_objects_already_present_in_r2
  [if] a same-size swap lands between the pre-upload hash and curl's read, a
       size-only listing accepts the corrupt object under the OLD digest's
       key -> test_swapped_during_upload_detects_a_changed_digest
  [if] the journal's last line is torn by a kill mid-append, the next run's
       read must not need manual repair -> test_journaled_mappings_tolerates_a_torn_final_line
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import b2_journal
from scripts.b2store import STORE_ROOT

#----- journal ------------------------------------------------------------

def test_legacy_key_strips_the_store_root() -> None:
    path = f"{STORE_ROOT}/stems/p/a/vocals.flac"
    assert b2_journal.legacy_key(path) == "stems/p/a/vocals.flac"


def test_journaled_mappings_is_empty_for_a_missing_journal(tmp_path: Path) -> None:
    assert b2_journal.journaled_mappings(tmp_path / "absent.jsonl") == set()


def test_journaled_mappings_reads_every_prior_run(tmp_path: Path) -> None:
    journal = tmp_path / "migration.jsonl"
    b2_journal.append_journal(
        journal,
        {"objects": [{"legacy_key": "stems/p/a/vocals.mp3", "key": "assets/aa/aa"}]},
    )
    b2_journal.append_journal(
        journal,
        {"objects": [{"legacy_key": "stems/p/b/drums.mp3", "key": "assets/bb/bb"}]},
    )
    assert b2_journal.journaled_mappings(journal) == {
        ("stems/p/a/vocals.mp3", "assets/aa/aa"),
        ("stems/p/b/drums.mp3", "assets/bb/bb"),
    }


def test_journaled_mappings_tolerates_a_torn_final_line(tmp_path: Path) -> None:
    """Regression guard: a journal whose last line was cut off mid-write (the
    process killed during append_journal's own write) must not block the
    next run's read, or this job's kill-and-resume design breaks."""
    journal = tmp_path / "migration.jsonl"
    b2_journal.append_journal(
        journal,
        {"objects": [{"legacy_key": "stems/p/a/vocals.mp3", "key": "assets/aa/aa"}]},
    )
    with journal.open("a") as handle:
        handle.write('{"objects": [{"legacy_key": "stems/p/b/drums.mp3"')  # torn
    assert b2_journal.journaled_mappings(journal) == {
        ("stems/p/a/vocals.mp3", "assets/aa/aa"),
    }


def test_journaled_mappings_still_raises_on_earlier_corruption(tmp_path: Path) -> None:
    """Built with a raw write, not two `append_journal` calls: this pins
    `journaled_mappings`'s own read-time contract on a fixed file state,
    independent of `append_journal`'s separate torn-tail repair (which would
    otherwise strip this exact corrupt line before it ever became
    non-final, defeating the setup)."""
    journal = tmp_path / "migration.jsonl"
    journal.write_text(
        '{"objects": [{"legacy_key": "stems/p/a/vocals.mp3"\n'
        '{"objects": [{"legacy_key": "stems/p/b/drums.mp3", "key": "assets/bb/bb"}]}\n'
    )
    with pytest.raises(json.JSONDecodeError):
        b2_journal.journaled_mappings(journal)


def test_truncate_torn_tail_leaves_the_original_intact_if_the_repair_write_fails(
    tmp_path: Path,
) -> None:
    """Regression guard: the repair used to call write_text() directly on the
    journal, which truncates the file to empty BEFORE writing the repaired
    content. A kill between that truncation and the write landing would lose
    every prior mapping, not just the torn line -- a more destructive window
    than the defect being repaired. The fix writes to a sibling temp file
    and renames it over the original; this proves that a failure writing the
    temp file leaves the original completely untouched, real filesystem
    only, no mocking: a directory occupying the temp path forces write_text
    to raise."""
    journal = tmp_path / "migration.jsonl"
    original = (
        '{"objects": [{"legacy_key": "stems/p/a/vocals.mp3", "key": "assets/aa/aa"}]}\n'
        '{"objects": [{"legacy_key": "stems/p/b/drums.mp3"'  # torn
    )
    journal.write_text(original)
    (tmp_path / "migration.jsonl.tmp").mkdir()  # occupies the repair's temp path
    with pytest.raises(IsADirectoryError):
        b2_journal._truncate_torn_tail(journal)
    assert journal.read_text() == original


def test_append_journal_repairs_a_torn_tail_before_writing(tmp_path: Path) -> None:
    """Regression guard: appending straight onto a torn final line (the
    process killed mid-write) concatenates this run's valid JSON onto
    invalid bytes with no separator, corrupting the NEW record too and
    silently losing every mapping recorded from that point forward."""
    journal = tmp_path / "migration.jsonl"
    journal.write_text('{"objects": [{"legacy_key": "stems/p/a/vocals.mp3"')  # torn
    b2_journal.append_journal(
        journal,
        {"objects": [{"legacy_key": "stems/p/b/drums.mp3", "key": "assets/bb/bb"}]},
    )
    assert b2_journal.journaled_mappings(journal) == {
        ("stems/p/b/drums.mp3", "assets/bb/bb"),
    }


def test_append_journal_inserts_a_missing_newline_before_appending(
    tmp_path: Path,
) -> None:
    """Regression guard: a kill can land right after the closing brace but
    before the trailing newline is flushed, leaving a COMPLETE, individually
    valid JSON object with no newline at end of file. json.loads tolerates
    that fine at read time, so the old torn-tail check treated it as
    well-formed and appended straight onto it -- concatenating the new
    record onto the old one with no separator (`{...}{...}`), which then
    reads back as one unparseable "line" and silently drops BOTH mappings."""
    journal = tmp_path / "migration.jsonl"
    journal.write_text(
        json.dumps({"objects": [{"legacy_key": "stems/p/a/vocals.mp3", "key": "assets/aa/aa"}]})
    )  # valid JSON, no trailing newline
    b2_journal.append_journal(
        journal,
        {"objects": [{"legacy_key": "stems/p/b/drums.mp3", "key": "assets/bb/bb"}]},
    )
    assert b2_journal.journaled_mappings(journal) == {
        ("stems/p/a/vocals.mp3", "assets/aa/aa"),
        ("stems/p/b/drums.mp3", "assets/bb/bb"),
    }


def test_append_journal_does_not_touch_a_well_formed_tail(tmp_path: Path) -> None:
    """The opposite direction: a normal, well-terminated prior run must not
    be touched by the torn-tail repair."""
    journal = tmp_path / "migration.jsonl"
    b2_journal.append_journal(
        journal,
        {"objects": [{"legacy_key": "stems/p/a/vocals.mp3", "key": "assets/aa/aa"}]},
    )
    b2_journal.append_journal(
        journal,
        {"objects": [{"legacy_key": "stems/p/b/drums.mp3", "key": "assets/bb/bb"}]},
    )
    assert b2_journal.journaled_mappings(journal) == {
        ("stems/p/a/vocals.mp3", "assets/aa/aa"),
        ("stems/p/b/drums.mp3", "assets/bb/bb"),
    }


def test_new_journal_mappings_includes_objects_already_present_in_r2() -> None:
    """Regression guard: scripts/b2_stems_to_r2.py:495 used to journal only
    the current run's `jobs`, so an object already present before the run
    (or one whose sibling PUT in the same batch failed, causing the run to
    exit before ever journaling) never got its legacy-to-content mapping
    recorded, permanently, despite R2 already holding the object."""
    already_present = ("stems/p/a/vocals.flac", "assets/aa/aa", 100)
    result = b2_journal.new_journal_mappings(
        planned=[already_present], final_remote={"assets/aa/aa": 100},
        already_journaled=set(),
    )
    assert result == [("stems/p/a/vocals.flac", "assets/aa/aa")]


def test_new_journal_mappings_skips_pairs_already_journaled() -> None:
    result = b2_journal.new_journal_mappings(
        planned=[("stems/p/a/vocals.flac", "assets/aa/aa", 100)],
        final_remote={"assets/aa/aa": 100},
        already_journaled={("stems/p/a/vocals.flac", "assets/aa/aa")},
    )
    assert result == []


def test_new_journal_mappings_excludes_a_short_object() -> None:
    result = b2_journal.new_journal_mappings(
        planned=[("stems/p/a/vocals.flac", "assets/aa/aa", 100)],
        final_remote={"assets/aa/aa": 3},
        already_journaled=set(),
    )
    assert result == []


def test_journal_new_mappings_excludes_swapped_keys(tmp_path: Path) -> None:
    """A key confirmed present at the right SIZE can still be corrupt if its
    source was swapped during upload; that mapping must not be journaled."""
    journal = tmp_path / "migration.jsonl"
    b2_journal.journal_new_mappings(
        journal,
        planned=[("stems/p/a/vocals.flac", "assets/aa/aa", 100)],
        remote={"assets/aa/aa": 100},
        already_journaled=set(),
        bucket="b",
        exclude_legacy_keys=frozenset({"stems/p/a/vocals.flac"}),
    )
    assert b2_journal.journaled_mappings(journal) == set()


#----- swap detection -------------------------------------------------------

def test_swapped_during_upload_detects_a_changed_digest() -> None:
    """Regression guard: bifrost2's rail hashes a source (`sha256sum`), then a
    separate `curl -T` invocation reopens it. A same-size swap in that window
    ships different bytes under the OLD digest's key, and a size-only R2
    listing cannot see that -- only a post-upload re-hash can."""
    jobs = [("/store/p/a/vocals.flac", "assets/aa/aa")]
    pre = {"/store/p/a/vocals.flac": "digest-before"}
    post = {"/store/p/a/vocals.flac": "digest-after"}
    assert b2_journal.swapped_during_upload(jobs, pre, post) == [
        "/store/p/a/vocals.flac"
    ]


def test_swapped_during_upload_is_empty_when_digests_match() -> None:
    jobs = [("/store/p/a/vocals.flac", "assets/aa/aa")]
    digests = {"/store/p/a/vocals.flac": "same-digest"}
    assert b2_journal.swapped_during_upload(jobs, digests, digests) == []
