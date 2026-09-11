"""Contract tests for the local -> R2 stem migration.

The transfer itself needs R2 credentials, but every decision the job makes
before a byte moves is pure and pinned here. Following the precedent set by
tests/scripts/test_b2_stems_to_r2.py, no S3 client is faked: a test built on a
stubbed client proves the stub behaves, not that the job is right.

  [if] the work set stops excluding size-matched objects, a re-run pushes
       13 GB again -> test_work_set_skips_objects_already_in_r2
  [if] presence alone starts satisfying the work set, a truncated upload is
       never repaired -> test_work_set_reuploads_a_short_object
  [if] a non-publishable bundle reaches the uploader, R2 gains an object no
       reader can load -> test_only_publishable_bundles_are_planned
  [if] a delete path is ever added, this job can destroy the only copy of a
       render -> test_no_delete_capability_exists
  [if] reconciliation stops comparing sizes, a short PUT passes as success
       -> test_reconcile_rejects_a_short_object
  [if] the journal stops appending, a run's history can be overwritten
       -> test_journal_is_append_only
  [if] upload_one re-opens its source to transfer it, a same-size swap
       between validation and the PUT ships the wrong bytes under the
       previous content-addressed key -> test_put_object_kwargs_ships_the_exact_bytes_it_hashed
  [if] the journal only records this run's batch, an already-present object
       (another rail, or a crash before the append) never gets its
       legacy-to-content mapping recorded
       -> test_new_journal_mappings_includes_objects_already_present_in_r2
  [if] the journal-append decision gates on new mappings alone, a run whose
       whole batch fails logs "nothing to do" and loses its only durable
       record of upload_errors/size_mismatches -> test_run_worth_recording_*
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
from pathlib import Path

import pytest

from scripts import local_stems_to_r2 as migrate
from scripts import r2_stems, stem_inventory
from scripts.local_stems_to_r2 import Upload

PRESET = "mel-band-roformer-ov8-seg256"
ID_A = "a" * 40


def _bundle_root(tmp_path: Path, *, manifest: bool = True) -> Path:
    bundle = tmp_path / ID_A
    bundle.mkdir(parents=True)
    (bundle / "vocals.mp3").write_bytes(b"v" * 100)
    (bundle / "instrumental.mp3").write_bytes(b"i" * 200)
    (bundle / "meta.json").write_text(json.dumps({"config_tag": PRESET}))
    if manifest:
        (bundle / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "layout": "roformer2",
                    "model": {"name": "mel-band-roformer", "version": PRESET},
                }
            )
        )
    return tmp_path


#----- planning ---------------------------------------------------------------

def test_only_publishable_bundles_are_planned(tmp_path: Path) -> None:
    """A manifestless bundle is complete but unloadable, so it must not be
    handed to the uploader."""
    root = _bundle_root(tmp_path, manifest=False)
    bundles = stem_inventory.scan_directory_bundles(root)
    assert bundles[0].is_complete
    assert migrate.planned_uploads(bundles) == []


def test_planned_uploads_cover_every_file_with_farm_keys(tmp_path: Path) -> None:
    root = _bundle_root(tmp_path)
    bundles = stem_inventory.scan_directory_bundles(root)
    planned = migrate.planned_uploads(bundles)
    keys = {item.key for item in planned}
    bodies = {name: path.read_bytes() for name, path in bundles[0].files.items()}
    assert keys == {
        migrate.object_key_for_body(body) for body in bodies.values()
    }
    assert all(item.size == item.source.stat().st_size for item in planned)


def test_content_type_is_set_per_extension(tmp_path: Path) -> None:
    root = _bundle_root(tmp_path)
    planned = migrate.planned_uploads(stem_inventory.scan_directory_bundles(root))
    by_suffix = {item.source.suffix: item.content_type for item in planned}
    assert by_suffix[".mp3"] == "audio/mpeg"
    assert by_suffix[".json"] == "application/json"


#----- the resumable work set -------------------------------------------------

def test_work_set_skips_objects_already_in_r2() -> None:
    planned = [Upload("assets/aa/aa", Path("/x"), 100)]
    assert migrate.outstanding(planned, {"assets/aa/aa": 100}) == []


def test_work_set_reuploads_a_short_object() -> None:
    """A truncated PUT still answers HTTP 200, so presence alone would let the
    next run skip exactly the object that needs redoing."""
    planned = [Upload("assets/aa/aa", Path("/x"), 100)]
    assert migrate.outstanding(planned, {"assets/aa/aa": 12}) == planned


def test_work_set_is_everything_when_r2_is_empty() -> None:
    planned = [
        Upload("assets/aa/aa", Path("/x"), 100),
        Upload("assets/bb/bb", Path("/y"), 200),
    ]
    assert migrate.outstanding(planned, {}) == planned


def test_rerun_after_success_is_a_no_op(tmp_path: Path) -> None:
    """Idempotence, end to end over the pure half: plan, pretend it landed,
    re-plan, and the second work set must be empty."""
    root = _bundle_root(tmp_path)
    planned = migrate.planned_uploads(stem_inventory.scan_directory_bundles(root))
    landed = {item.key: item.size for item in planned}
    assert migrate.outstanding(planned, landed) == []


def test_key_is_the_sha256_of_the_source_body(tmp_path: Path) -> None:
    source = tmp_path / "vocals.flac"
    body = b"actual audio bytes"
    source.write_bytes(body)
    item = migrate.upload_for_source(source)
    digest = hashlib.sha256(body).hexdigest()
    assert item.key == f"assets/{digest[:2]}/{digest}"


def test_upload_rejects_a_source_changed_after_planning(tmp_path: Path) -> None:
    source = tmp_path / "vocals.flac"
    source.write_bytes(b"first render")
    planned = migrate.upload_for_source(source)
    source.write_bytes(b"replacement render")
    with pytest.raises(RuntimeError, match="changed after content-addressing"):
        migrate.upload_one(object(), "bucket", planned)


def test_put_object_kwargs_ships_the_exact_bytes_it_hashed(tmp_path: Path) -> None:
    """Regression guard: scripts/local_stems_to_r2.py:upload_one used to
    validate a source's bytes and then hand `client.upload_file` the PATH,
    which reopens it a second time. A same-size swap in that window would
    ship bytes that no longer match the content-addressed key.

    Pure, no client of any kind: exercising `client.put_object` itself needs
    real R2 credentials this suite does not have (see module docstring) and
    boto3 is deliberately absent from this repo's venv (a PEP 723 inline
    dependency), so a fake standing in for it would fabricate the very
    capability this file's own docstring says is unavailable here. This
    proves everything decided BEFORE that call -- the exact bug's location --
    by asserting `Body` is the bytes already read, not the path: a
    regression to `upload_file(path)` orphans `_put_object_kwargs` entirely,
    and a regression to `Body=item.source` fails the `isinstance` check."""
    source = tmp_path / "vocals.flac"
    body = b"validated render"
    source.write_bytes(body)
    item = migrate.upload_for_source(source)

    kwargs = migrate._put_object_kwargs(item)
    assert isinstance(kwargs["Body"], bytes)
    assert kwargs["Body"] == body
    assert kwargs["Key"] == item.key
    assert kwargs["ContentType"] == item.content_type


#----- verification -----------------------------------------------------------

def test_reconcile_confirms_matching_sizes() -> None:
    attempted = [Upload("k", Path("/x"), 100)]
    confirmed, bad = migrate.reconcile(attempted, {"k": 100})
    assert confirmed == attempted
    assert bad == []


def test_reconcile_rejects_a_short_object() -> None:
    attempted = [Upload("k", Path("/x"), 100)]
    confirmed, bad = migrate.reconcile(attempted, {"k": 3})
    assert confirmed == []
    assert "expected 100 bytes, R2 has 3" in bad[0]


def test_reconcile_rejects_a_missing_object() -> None:
    attempted = [Upload("k", Path("/x"), 100)]
    confirmed, bad = migrate.reconcile(attempted, {})
    assert confirmed == []
    assert "R2 has None" in bad[0]


#----- safety -----------------------------------------------------------------

def test_no_delete_capability_exists() -> None:
    """This job publishes the only copy of renders that cost GPU hours, so
    deletion must not be a mode it can be talked into. Its sibling's
    --delete-after is safe there and would not be here.

    Checked against the parsed call graph rather than the source text: the
    module's own prose explains why it does not delete, and a grep would
    match that explanation and fail on a correct file.
    """
    destructive = {"delete_object", "delete_objects", "unlink", "rmtree", "remove"}
    called: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(migrate))):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Attribute):
            called.add(target.attr)
        elif isinstance(target, ast.Name):
            called.add(target.id)
    assert not (called & destructive), f"destructive calls present: {called & destructive}"


def test_parser_offers_no_delete_flag() -> None:
    options = {
        option
        for action in migrate.build_parser()._actions
        for option in action.option_strings
    }
    assert not any("delete" in option for option in options)
    assert "--dry-run" in options


#----- journal ----------------------------------------------------------------

def test_journal_is_append_only(tmp_path: Path) -> None:
    journal = tmp_path / "nested" / "migration.jsonl"
    migrate.append_journal(journal, {"attempted": 1})
    migrate.append_journal(journal, {"attempted": 2})
    lines = journal.read_text().strip().splitlines()
    assert [json.loads(line)["attempted"] for line in lines] == [1, 2]


def test_journaled_mappings_is_empty_for_a_missing_journal(tmp_path: Path) -> None:
    assert migrate._journaled_mappings(tmp_path / "absent.jsonl") == set()


def test_journaled_mappings_reads_every_prior_run(tmp_path: Path) -> None:
    journal = tmp_path / "migration.jsonl"
    migrate.append_journal(
        journal,
        {"objects": [{"legacy_key": "stems/p/a/vocals.mp3", "key": "assets/aa/aa"}]},
    )
    migrate.append_journal(
        journal,
        {"objects": [{"legacy_key": "stems/p/b/drums.mp3", "key": "assets/bb/bb"}]},
    )
    assert migrate._journaled_mappings(journal) == {
        ("stems/p/a/vocals.mp3", "assets/aa/aa"),
        ("stems/p/b/drums.mp3", "assets/bb/bb"),
    }


def test_journaled_mappings_tolerates_a_torn_final_line(tmp_path: Path) -> None:
    """Regression guard: scripts/local_stems_to_r2.py:271 raised
    JSONDecodeError on a journal whose last line was cut off mid-write (the
    process killed during append_journal's own write). That contradicts this
    job's kill-and-resume design: the very next run needs to read the
    journal, not need manual repair first."""
    journal = tmp_path / "migration.jsonl"
    migrate.append_journal(
        journal,
        {"objects": [{"legacy_key": "stems/p/a/vocals.mp3", "key": "assets/aa/aa"}]},
    )
    with journal.open("a") as handle:
        handle.write('{"objects": [{"legacy_key": "stems/p/b/drums.mp3"')  # torn
    assert migrate._journaled_mappings(journal) == {
        ("stems/p/a/vocals.mp3", "assets/aa/aa"),
    }


def test_journaled_mappings_still_raises_on_earlier_corruption(tmp_path: Path) -> None:
    """The opposite direction: only the FINAL line may be torn. A corrupt
    line earlier in the file is real corruption, not an interrupted write,
    and must still raise rather than be silently skipped.

    Built with a raw write, not two `append_journal` calls: this pins
    `_journaled_mappings`'s own read-time contract on a fixed file state,
    independent of `append_journal`'s separate torn-tail repair (which would
    otherwise strip this exact corrupt line before it ever became
    non-final, defeating the setup)."""
    journal = tmp_path / "migration.jsonl"
    journal.write_text(
        '{"objects": [{"legacy_key": "stems/p/a/vocals.mp3"\n'
        '{"objects": [{"legacy_key": "stems/p/b/drums.mp3", "key": "assets/bb/bb"}]}\n'
    )
    with pytest.raises(json.JSONDecodeError):
        migrate._journaled_mappings(journal)


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
        migrate._truncate_torn_tail(journal)
    assert journal.read_text() == original


def test_append_journal_repairs_a_torn_tail_before_writing(tmp_path: Path) -> None:
    """Regression guard: appending straight onto a torn final line (the
    process killed mid-write) concatenates this run's valid JSON onto
    invalid bytes with no separator, corrupting the NEW record too and
    silently losing every mapping recorded from that point forward."""
    journal = tmp_path / "migration.jsonl"
    journal.write_text('{"objects": [{"legacy_key": "stems/p/a/vocals.mp3"')  # torn
    migrate.append_journal(
        journal,
        {"objects": [{"legacy_key": "stems/p/b/drums.mp3", "key": "assets/bb/bb"}]},
    )
    assert migrate._journaled_mappings(journal) == {
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
    migrate.append_journal(
        journal,
        {"objects": [{"legacy_key": "stems/p/b/drums.mp3", "key": "assets/bb/bb"}]},
    )
    assert migrate._journaled_mappings(journal) == {
        ("stems/p/a/vocals.mp3", "assets/aa/aa"),
        ("stems/p/b/drums.mp3", "assets/bb/bb"),
    }


def test_append_journal_does_not_touch_a_well_formed_tail(tmp_path: Path) -> None:
    """The opposite direction: a normal, well-terminated prior run must not
    be touched by the torn-tail repair."""
    journal = tmp_path / "migration.jsonl"
    migrate.append_journal(
        journal,
        {"objects": [{"legacy_key": "stems/p/a/vocals.mp3", "key": "assets/aa/aa"}]},
    )
    migrate.append_journal(
        journal,
        {"objects": [{"legacy_key": "stems/p/b/drums.mp3", "key": "assets/bb/bb"}]},
    )
    assert migrate._journaled_mappings(journal) == {
        ("stems/p/a/vocals.mp3", "assets/aa/aa"),
        ("stems/p/b/drums.mp3", "assets/bb/bb"),
    }


def test_new_journal_mappings_includes_objects_already_present_in_r2() -> None:
    """Regression guard: scripts/local_stems_to_r2.py:317 used to journal only
    `confirmed` (this run's uploaded batch), so an object another rail already
    published, or one that landed just before a crash cut off the append, was
    excluded from `todo` on the next run and never journaled at all -- a
    permanent loss of its legacy-to-content mapping despite a successful,
    no-op rerun."""
    already_present = Upload(
        "assets/aa/aa", Path("/x"), 100, legacy_key="stems/p/a/vocals.mp3"
    )
    result = migrate.new_journal_mappings(
        planned=[already_present],
        final_remote={"assets/aa/aa": 100},
        already_journaled=set(),
    )
    assert result == [already_present]


def test_new_journal_mappings_skips_pairs_already_journaled() -> None:
    item = Upload("assets/aa/aa", Path("/x"), 100, legacy_key="stems/p/a/vocals.mp3")
    already_journaled = {(item.legacy_key, item.key)}
    result = migrate.new_journal_mappings(
        planned=[item], final_remote={"assets/aa/aa": 100},
        already_journaled=already_journaled,
    )
    assert result == []


def test_new_journal_mappings_rejournals_a_re_rendered_source() -> None:
    """A re-render keeps the same legacy_key but changes the content key;
    deduping by legacy_key alone would silently drop the new mapping."""
    old_pair = ("stems/p/a/vocals.mp3", "assets/aa/aa")
    re_rendered = Upload(
        "assets/bb/bb", Path("/x"), 100, legacy_key="stems/p/a/vocals.mp3"
    )
    result = migrate.new_journal_mappings(
        planned=[re_rendered],
        final_remote={"assets/bb/bb": 100},
        already_journaled={old_pair},
    )
    assert result == [re_rendered]


def test_new_journal_mappings_excludes_objects_not_yet_confirmed() -> None:
    item = Upload("assets/aa/aa", Path("/x"), 100, legacy_key="stems/p/a/vocals.mp3")
    result = migrate.new_journal_mappings(
        planned=[item], final_remote={}, already_journaled=set()
    )
    assert result == []


def test_new_journal_mappings_excludes_a_short_object() -> None:
    """A key present at the wrong size is not confirmed. Without this case,
    `final_remote.get(item.key) == item.size` could regress to
    `item.key in final_remote` (presence alone) and this test suite would not
    notice, exactly the size-vs-presence mistake reconcile() already guards
    against on the upload side."""
    item = Upload("assets/aa/aa", Path("/x"), 100, legacy_key="stems/p/a/vocals.mp3")
    result = migrate.new_journal_mappings(
        planned=[item], final_remote={"assets/aa/aa": 3}, already_journaled=set()
    )
    assert result == []


def test_run_worth_recording_when_new_mappings_exist() -> None:
    assert migrate.run_worth_recording(batch=[], new_mappings=[Upload("k", Path("/x"), 1)])


def test_run_worth_recording_when_a_batch_was_attempted_but_nothing_confirmed() -> None:
    """Regression guard: a run whose entire batch fails or lands short must
    still be journaled, or its upload_errors/size_mismatches are lost and the
    run misreports itself as 'nothing to do'."""
    attempted = [Upload("k", Path("/x"), 1)]
    assert migrate.run_worth_recording(batch=attempted, new_mappings=[])


def test_run_worth_recording_false_when_genuinely_idle() -> None:
    assert not migrate.run_worth_recording(batch=[], new_mappings=[])


def test_dry_run_needs_no_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The dry run must inspect only the local side, so the inventory stays
    usable while the R2 token is still unminted."""
    root = _bundle_root(tmp_path)
    for key in r2_stems.R2_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    exit_code = migrate.main(
        ["--root", str(root), "--data-dir", str(tmp_path / "nodata"), "--dry-run"]
    )
    assert exit_code == 0
    assert "nothing uploaded" in capsys.readouterr().out
