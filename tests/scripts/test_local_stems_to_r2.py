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
"""

from __future__ import annotations

import ast
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
    assert keys == {
        f"stems/{PRESET}/{ID_A}/vocals.mp3",
        f"stems/{PRESET}/{ID_A}/instrumental.mp3",
        f"stems/{PRESET}/{ID_A}/manifest.json",
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
    planned = [Upload("stems/p/a/vocals.mp3", Path("/x"), 100)]
    assert migrate.outstanding(planned, {"stems/p/a/vocals.mp3": 100}) == []


def test_work_set_reuploads_a_short_object() -> None:
    """A truncated PUT still answers HTTP 200, so presence alone would let the
    next run skip exactly the object that needs redoing."""
    planned = [Upload("stems/p/a/vocals.mp3", Path("/x"), 100)]
    assert migrate.outstanding(planned, {"stems/p/a/vocals.mp3": 12}) == planned


def test_work_set_is_everything_when_r2_is_empty() -> None:
    planned = [
        Upload("stems/p/a/vocals.mp3", Path("/x"), 100),
        Upload("stems/p/a/instrumental.mp3", Path("/y"), 200),
    ]
    assert migrate.outstanding(planned, {}) == planned


def test_rerun_after_success_is_a_no_op(tmp_path: Path) -> None:
    """Idempotence, end to end over the pure half: plan, pretend it landed,
    re-plan, and the second work set must be empty."""
    root = _bundle_root(tmp_path)
    planned = migrate.planned_uploads(stem_inventory.scan_directory_bundles(root))
    landed = {item.key: item.size for item in planned}
    assert migrate.outstanding(planned, landed) == []


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
