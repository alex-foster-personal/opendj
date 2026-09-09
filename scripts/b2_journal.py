#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Journal bookkeeping for scripts/b2_stems_to_r2.py's bifrost2 -> R2 backfill.

Split out of that script purely to keep it under this repo's 600-line
file-size gate: these are the pure, no-network pieces of "which
legacy-to-content mappings does this run still need to record."

  [if] the journal only records jobs uploaded THIS run, an already-present
       object or one whose sibling PUT failed never gets its mapping
       recorded [then ⛔️] test_new_journal_mappings_includes_objects_already_present_in_r2
  [if] a same-size swap lands between the pre-upload hash and curl's read
       [then ⛔️] test_swapped_during_upload_detects_a_changed_digest catches
       it via a post-upload re-hash, invisible to a size-only listing
  [if] the journal's last line is torn by a kill mid-append [then ⛔️]
       test_journaled_mappings_tolerates_a_torn_final_line proves the next
       run's read does not need manual repair, while
       test_journaled_mappings_still_raises_on_earlier_corruption proves an
       earlier corrupt line still raises
  [if] a new append lands on top of that same torn tail [then ⛔️]
       test_append_journal_repairs_a_torn_tail_before_writing proves the
       WRITER truncates the torn line first (a stderr note names what was
       discarded), instead of concatenating valid JSON onto invalid bytes
       and losing every mapping appended after it, while
       test_append_journal_does_not_touch_a_well_formed_tail proves a normal
       prior run is left alone
  [if] a kill lands right after the final closing brace but before its
       newline is flushed [then ⛔️] test_append_journal_inserts_a_missing_newline_before_appending
       proves the WRITER inserts the missing separator before appending,
       instead of concatenating the new record onto the old one with no
       separator (which reads back as one unparseable line and silently
       drops both mappings, even though the prior append individually
       reported success)
  [if] the repair write itself is interrupted [then ⛔️]
       test_truncate_torn_tail_leaves_the_original_intact_if_the_repair_write_fails
       proves the repair writes to a sibling temp file and renames it over
       the original, instead of truncating the journal in place, so a kill
       mid-repair cannot lose every prior mapping (not just the torn line)

-Claude
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

from scripts.b2store import STORE_ROOT


def append_journal(path: Path, record: dict[str, Any]) -> None:
    """Append durable source-path to content-key mappings for this publish.

    Repairs a torn tail left by a kill mid-append first: appending straight
    onto a truncated final line would concatenate this run's valid JSON onto
    invalid bytes with no separator, corrupting the new record too and
    silently swallowing every mapping recorded from this point forward.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _truncate_torn_tail(path)
    with path.open("a") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _truncate_torn_tail(path: Path) -> None:
    """Repair a journal so the next append never lands mid-line.

    Two shapes come out of a kill mid-write: a genuinely torn line (drop it,
    only ever touching the LAST line -- an earlier corrupt line is real
    corruption and stays in place to raise), and a complete-but-unterminated
    final object whose trailing newline never landed. Read-time tolerance
    treats the second as fine, since json.loads ignores a missing trailing
    newline, but appending straight onto it would concatenate the new record
    right after the old one with no separator, corrupting both on the next
    read.
    """
    if not path.is_file():
        return
    lines = path.read_text().splitlines(keepends=True)
    if not lines:
        return
    try:
        json.loads(lines[-1])
    except json.JSONDecodeError:
        print(
            f"b2_journal: discarding torn tail line from {path}: {lines[-1]!r}",
            file=sys.stderr,
        )
        # write_text() truncates in place first: a kill between that
        # truncation and the write landing would lose every prior mapping,
        # not just the torn line. Write the repair to a sibling temp file
        # and rename it over the original, so a kill mid-repair leaves
        # either the untouched original or the fully-written repair, never
        # a partial file.
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("".join(lines[:-1]))
        tmp.replace(path)
        return
    if not lines[-1].endswith("\n"):
        with path.open("a") as handle:
            handle.write("\n")


def journaled_mappings(path: Path) -> set[tuple[str, str]]:
    """(legacy_key, key) pairs already recorded across every prior run.

    Paired identity, not legacy_key alone: a re-render keeps the same
    legacy_key but changes its content key, and a legacy_key-only dedup
    would silently drop that new mapping forever.

    Tolerates a torn FINAL line only (a kill mid-append), matching this
    job's kill-and-resume design; an earlier corrupt line still raises.
    """
    if not path.is_file():
        return set()
    mappings: set[tuple[str, str]] = set()
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    for index, line in enumerate(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1:
                break
            raise
        for obj in record.get("objects", []):
            mappings.add((obj["legacy_key"], obj["key"]))
    return mappings


def legacy_key(remote_path: str) -> str:
    """The journal's stable identity for a bifrost2 path: relative to the
    asset store root, independent of the Windows drive prefix."""
    return remote_path.removeprefix(f"{STORE_ROOT}/")


def new_journal_mappings(
    planned: list[tuple[str, str, int]],
    final_remote: dict[str, int],
    already_journaled: set[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Every planned (legacy_key, key, size) confirmed in R2 right now and
    not already journaled.

    Mirrors scripts/local_stems_to_r2.py's helper of the same name:
    ``planned`` spans every source in the batch, not only this run's
    uploads, so an already-present object still gets journaled.
    """
    return [
        (lk, key)
        for lk, key, size in planned
        if final_remote.get(key) == size and (lk, key) not in already_journaled
    ]


def journal_new_mappings(
    journal: Path,
    planned: list[tuple[str, str, int]],
    remote: dict[str, int],
    already_journaled: set[tuple[str, str]],
    bucket: str,
    exclude_legacy_keys: frozenset[str] = frozenset(),
) -> None:
    """Journal every newly-confirmed (legacy_key, key) pair.

    ``exclude_legacy_keys`` drops any object swapped during this run's
    upload: present at the right SIZE, but the bytes no longer match the
    key, so recording it would journal a corrupt mapping.
    """
    new_mappings = [
        (lk, k)
        for lk, k in new_journal_mappings(planned, remote, already_journaled)
        if lk not in exclude_legacy_keys
    ]
    if not new_mappings:
        return
    append_journal(
        journal,
        {
            "at": dt.datetime.now(dt.UTC).isoformat(),
            "bucket": bucket,
            "objects": [{"legacy_key": lk, "key": k} for lk, k in new_mappings],
        },
    )


def swapped_during_upload(
    jobs: list[tuple[str, str]],
    pre_digests: dict[str, str],
    post_digests: dict[str, str],
) -> list[str]:
    """Remote paths whose bifrost2 bytes changed between the pre-upload hash
    and a re-hash taken right after upload.

    A same-size swap in that window ships wrong bytes under the OLD digest's
    key, invisible to a size-only R2 listing; this closes that gap.
    """
    return [
        remote_path
        for remote_path, _ in jobs
        if post_digests.get(remote_path) != pre_digests.get(remote_path)
    ]
