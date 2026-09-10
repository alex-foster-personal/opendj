"""Migration logic: legacy ``stems/...`` objects onto the content-addressed key.

The repo held two R2 layouts until cloudsync issue #1452 chose content
addressing. Objects already written under ``stems/<preset>/<stable_id>/<file>``
need re-keying to ``assets/<sha256[:2]>/<sha256>``; this module is that move.

Acceptances (one assertion block each):

* a legacy key is deleted only AFTER the new object verifies byte-identical -
  if delete happened first, an interrupted migration loses the only copy --
  broken.
* a dry run reads and hashes real bodies but writes/deletes nothing - if a
  dry run mutated the store, the plan and the live run diverge -- broken.
* a non-stems key under the scanned prefix is reported, never silently
  dropped or guessed into a new key -- broken.
* a live ``--delete-after`` run REQUIRES a durable ledger key and writes the
  ledger before deleting any legacy key - content-addressed keys carry no
  preset/stable_id/filename, so without the ledger the deletion destroys the
  only bundle mapping -- broken.
* a live ``--delete-after`` run whose ledger_key already holds a ledger
  EXTENDS it (unioned by old_key), never overwrites it, so a second ``--limit``
  batch cannot erase the first batch's mapping - an unreadable or
  cross-campaign existing ledger refuses the delete -- broken.
* a ledger_key under the scanned prefix is REFUSED, and the ledger key is
  never in the migration scan - a ledger under ``stems/`` would be re-scanned
  as a legacy object and deleted on the next run, taking the only mapping with
  it -- broken.
* an already-ledgered old_key whose content changed between runs (new run
  derives a different new_key) ABORTS the delete as a conflict - an old_key-
  only union would silently drop the new mapping and delete the legacy key
  under a stale ledger -- broken. An identical repeat is NOT a conflict.
* a legacy key whose new object fails byte-identical verification is KEPT and
  REPORTED as a failure AND omitted from the durable ledger - silently skipping
  the delete would make an incomplete migration look finished (failures=0, exit
  0), and advertising a corrupt destination as migrated would let a later
  backfill publish an unusable object -- broken.
* a legacy key whose source changed between the read and the delete is KEPT and
  REPORTED - deleting it would drop the newer generation under a ledger that
  points at the older bytes -- broken.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from apps.cloud.legacy_stems_migration import (
    REKEY_LEDGER_LAYOUT,
    migrate_legacy_stems,
    rekey_plan,
)


@dataclass
class DictStore:
    """A dict-backed object store with the narrow migration surface."""

    objects: dict[str, bytes] = field(default_factory=dict)

    def list_objects(self, bucket: str, prefix: str) -> list[str]:
        _ = bucket
        return sorted(key for key in self.objects if key.startswith(prefix))

    def get_object(self, bucket: str, key: str) -> bytes | None:
        _ = bucket
        return self.objects.get(key)

    def put_object(self, bucket: str, key: str, body: bytes) -> None:
        _ = bucket
        self.objects[key] = body

    def delete_object(self, bucket: str, key: str) -> None:
        _ = bucket
        self.objects.pop(key, None)


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _legacy(preset: str, stable_id: str, filename: str) -> str:
    return f"stems/{preset}/{stable_id}/{filename}"


def _asset(body: bytes) -> str:
    digest = _sha(body)
    return f"assets/{digest[:2]}/{digest}"


BODY = b"rendered vocal flac bytes"
STABLE = "ab12" * 10


def test_dry_run_reads_and_hashes_but_writes_nothing():
    store = DictStore({_legacy("htdemucs", STABLE, "vocals.flac"): BODY})
    result = migrate_legacy_stems(store, "music-dj-audio", dry_run=True)
    assert result.migrated == 1
    assert len(result.rekeyed) == 1
    assert result.rekeyed[0][0] == _legacy("htdemucs", STABLE, "vocals.flac")
    assert result.rekeyed[0][1] == _asset(BODY)
    # Dry run must not write the new key NOR delete the old one.
    assert set(store.objects) == {_legacy("htdemucs", STABLE, "vocals.flac")}


def test_live_writes_the_new_key_and_keeps_the_legacy_one_by_default():
    legacy_key = _legacy("htdemucs", STABLE, "vocals.flac")
    store = DictStore({legacy_key: BODY})
    result = migrate_legacy_stems(store, "bucket", dry_run=False)
    assert result.migrated == 1
    assert store.objects[legacy_key] == BODY
    assert store.objects[_asset(BODY)] == BODY


LEDGER_KEY = "_ledgers/rekey-2026.json"


def test_delete_after_without_a_ledger_key_refuses_loudly():
    # A live destructive run with no ledger would delete the only mapping
    # from preset/stable_id/filename to the opaque content address. Fail
    # fast instead of shipping a silent destructive default.
    store = DictStore({_legacy("htdemucs", STABLE, "vocals.flac"): BODY})
    try:
        migrate_legacy_stems(
            store, "bucket", dry_run=False, delete_after=True
        )
    except ValueError as exc:
        assert "ledger_key" in str(exc)
    else:
        raise AssertionError("delete_after without ledger_key must raise")


def test_delete_after_writes_the_ledger_then_removes_a_verified_legacy_copy():
    legacy_key = _legacy("htdemucs", STABLE, "drums.flac")
    store = DictStore({legacy_key: BODY})
    result = migrate_legacy_stems(
        store, "bucket", dry_run=False, delete_after=True, ledger_key=LEDGER_KEY
    )
    assert result.migrated == 1
    assert result.deleted == 1
    assert result.ledger_key == LEDGER_KEY
    assert legacy_key not in store.objects
    assert store.objects[_asset(BODY)] == BODY
    # The ledger object durably records the old -> new mapping INCLUDING the
    # parsed bundle identity, so the association survives the deletion.
    ledger = json.loads(store.objects[LEDGER_KEY].decode("utf-8"))
    assert ledger["entries"] == [
        {
            "old_key": legacy_key,
            "new_key": _asset(BODY),
            "preset": "htdemucs",
            "stable_id": STABLE,
            "filename": "drums.flac",
        }
    ]


def test_a_ledger_key_under_the_scanned_prefix_is_refused():
    legacy_key = _legacy("htdemucs", STABLE, "drums.flac")

    # A ledger inside the scanned namespace would be re-scanned as a legacy
    # object on the NEXT run and, with delete_after, deleted - the ledger is
    # the only record of the bundle mapping, so it must live outside the
    # prefix it describes.
    store = DictStore({legacy_key: BODY})
    try:
        migrate_legacy_stems(
            store, "bucket", dry_run=False, delete_after=True,
            ledger_key="stems/migrations/run/ledger.json",
        )
    except ValueError as exc:
        assert "ledger_key" in str(exc)
        assert "namespace" in str(exc)
    else:
        raise AssertionError(
            "a delete_after ledger_key under the scanned prefix must raise"
        )


def test_a_ledger_anywhere_in_the_legacy_namespace_is_refused_even_on_a_narrowed_prefix():
    # A narrowed --prefix batch must not be able to place its ledger under the
    # wider legacy namespace: a later DEFAULT run scans stems/ and would treat
    # that ledger as a legacy object and delete it. The guard keys on the whole
    # namespace, not the run's own prefix.
    legacy_key = _legacy("htdemucs", STABLE, "drums.flac")
    store = DictStore({legacy_key: BODY})
    try:
        migrate_legacy_stems(
            store, "bucket", dry_run=False, delete_after=True,
            prefix="stems/htdemucs/",
            ledger_key="stems/migrations/run/ledger.json",
        )
    except ValueError as exc:
        assert "ledger_key" in str(exc)
        assert "namespace" in str(exc)
    else:
        raise AssertionError(
            "a ledger under the legacy namespace must be refused even when "
            "the run's own prefix is narrower"
        )


def test_a_ledger_key_never_enters_the_migration_scan():
    # The keys= override can name the ledger key itself; exclude it so a
    # delete_after run can never migrate or delete its own index object.
    legacy_key = _legacy("htdemucs", STABLE, "drums.flac")
    store = DictStore({legacy_key: BODY})
    store.objects[LEDGER_KEY] = _ledger_for("bucket")
    result = migrate_legacy_stems(
        store, "bucket", dry_run=False, delete_after=True,
        ledger_key=LEDGER_KEY, keys=[legacy_key, LEDGER_KEY],
    )
    assert result.deleted == 1
    assert result.failures == ()
    assert legacy_key not in store.objects
    assert LEDGER_KEY in store.objects  # the ledger survived its own run


def test_a_short_write_of_the_new_object_is_kept_and_reported_not_silently_deleted():
    legacy_key = _legacy("htdemucs", STABLE, "drums.flac")

    # A store that truncates the DESTINATION put (a short PUT, the failure
    # mode a real object store produces) must not let the old key be deleted:
    # that is the one irreversible failure mode. It must also be REPORTED: a
    # silent skip would print failures=0 and exit 0 on an incomplete
    # migration. And the unverified mapping must not reach the ledger: a later
    # backfill reading it would publish an object that fails the digest its
    # address advertises.
    class ShortWriteDestination(DictStore):
        def put_object(self, bucket: str, key: str, body: bytes) -> None:
            if key.startswith("assets/"):
                self.objects[key] = body[:-1]
            else:
                self.objects[key] = body

    short = ShortWriteDestination({legacy_key: BODY})
    result = migrate_legacy_stems(
        short, "bucket", dry_run=False, delete_after=True, ledger_key=LEDGER_KEY
    )
    assert result.deleted == 0
    assert legacy_key in short.objects  # old copy survived the short write
    assert short.objects[_asset(BODY)] != BODY  # new copy is truncated
    assert len(result.failures) == 1
    assert "failed byte-identical verification" in result.failures[0]
    assert LEDGER_KEY not in short.objects  # nothing verified -> nothing ledgered


def test_a_failed_ledger_write_aborts_all_deletes():
    legacy_key = _legacy("htdemucs", STABLE, "drums.flac")

    # A store that truncates the LEDGER put makes the ledger unreadable. The
    # run must not delete anything: without a durable mapping the delete is
    # irreversible AND unrecorded.
    class TruncateLedger(DictStore):
        def put_object(self, bucket: str, key: str, body: bytes) -> None:
            if key == LEDGER_KEY:
                self.objects[key] = body[:-1]
            else:
                self.objects[key] = body

    store = TruncateLedger({legacy_key: BODY})
    result = migrate_legacy_stems(
        store, "bucket", dry_run=False, delete_after=True, ledger_key=LEDGER_KEY
    )
    assert result.deleted == 0
    assert result.migrated == 1
    assert legacy_key in store.objects
    assert len(result.failures) == 1
    assert "ledger did not persist" in result.failures[0]


def test_a_failed_second_batch_restores_the_prior_ledger_not_destroying_it():
    # Batch 1 persists a durable ledger and deletes its legacy key. Batch 2
    # reuses the key and its ledger PUT truncates: the unconditional overwrite
    # would otherwise clobber batch 1's mapping, whose legacy key is ALREADY
    # gone - reporting batch 2's failure would not bring that mapping back.
    # The verified prior body must be restored so batch 1 stays recorded.
    k1 = _legacy("htdemucs", STABLE, "vocals.flac")
    k2 = _legacy("htdemucs", "cd34" * 10, "vocals.flac")

    class TruncatesSecondLedgerPut(DictStore):
        """Truncate only the SECOND ledger put: batch 1 persists, batch 2's
        overwrite corrupts, and the restore put (the third) succeeds."""

        def __init__(self, initial):
            super().__init__(initial)
            self._ledger_puts = 0

        def put_object(self, bucket: str, key: str, body: bytes) -> None:
            if key == LEDGER_KEY:
                self._ledger_puts += 1
                if self._ledger_puts == 2:
                    self.objects[key] = body[:-1]
                    return
            self.objects[key] = body

    store = TruncatesSecondLedgerPut({k1: BODY, k2: BODY})
    first = migrate_legacy_stems(
        store, "bucket", dry_run=False, delete_after=True,
        ledger_key=LEDGER_KEY, keys=[k1],
    )
    assert first.deleted == 1
    prior_ledger = store.objects[LEDGER_KEY]

    second = migrate_legacy_stems(
        store, "bucket", dry_run=False, delete_after=True,
        ledger_key=LEDGER_KEY, keys=[k2],
    )
    assert second.deleted == 0
    assert k2 in store.objects  # not deleted under the failed overwrite
    assert store.objects[LEDGER_KEY] == prior_ledger  # restored, not clobbered
    entries = json.loads(store.objects[LEDGER_KEY].decode("utf-8"))["entries"]
    assert {entry["old_key"] for entry in entries} == {k1}
    assert any("did not persist" in failure for failure in second.failures)
    assert any("restore attempted" in failure for failure in second.failures)


def test_reusing_a_ledger_key_across_batches_extends_it_not_overwrites():
    # A delete_after campaign may span several --limit batches sharing one
    # ledger key. The second batch must UNION into the first, never replace:
    # batch 1's legacy keys are already gone, so an overwrite would destroy
    # the only bundle association those objects had.
    k1 = _legacy("htdemucs", STABLE, "vocals.flac")
    k2 = _legacy("htdemucs", "cd34" * 10, "vocals.flac")
    store = DictStore({k1: BODY, k2: BODY})
    first = migrate_legacy_stems(
        store, "bucket", dry_run=False, delete_after=True,
        ledger_key=LEDGER_KEY, keys=[k1],
    )
    assert first.deleted == 1
    second = migrate_legacy_stems(
        store, "bucket", dry_run=False, delete_after=True,
        ledger_key=LEDGER_KEY, keys=[k2],
    )
    assert second.deleted == 1
    entries = json.loads(store.objects[LEDGER_KEY].decode("utf-8"))["entries"]
    assert {entry["old_key"] for entry in entries} == {k1, k2}
    assert k1 not in store.objects and k2 not in store.objects


def test_a_conflicting_remap_of_an_already_ledgered_old_key_aborts_the_delete():
    # Batch 1 verifies its destination and deletes the legacy key under a
    # durable ledger mapping. A retained archival uploader then re-creates the
    # SAME stems/ key with different bytes. Batch 2 must not delete that newer
    # generation under the stale mapping: the ledger would point at the old
    # object forever. The old_key-only union would have silently dropped the
    # new digest and deleted anyway.
    legacy_key = _legacy("htdemucs", STABLE, "drums.flac")
    replaced = b"re-rendered vocal flac bytes, different content"
    store = DictStore({legacy_key: BODY})
    first = migrate_legacy_stems(
        store, "bucket", dry_run=False, delete_after=True, ledger_key=LEDGER_KEY
    )
    assert first.deleted == 1
    assert legacy_key not in store.objects

    store.objects[legacy_key] = replaced  # stale uploader re-creates the key
    second = migrate_legacy_stems(
        store, "bucket", dry_run=False, delete_after=True, ledger_key=LEDGER_KEY
    )
    assert second.deleted == 0
    assert store.objects[legacy_key] == replaced  # newer generation survives
    assert any("already maps to" in failure for failure in second.failures)
    entries = json.loads(store.objects[LEDGER_KEY].decode("utf-8"))["entries"]
    assert entries[0]["new_key"] == _asset(BODY)  # ledger is unchanged


def test_an_identical_repeat_of_an_already_ledgered_mapping_is_not_a_conflict():
    # Negative control for the conflict guard: the SAME bytes re-created under
    # an already-ledgered legacy key derive the SAME new_key, so the merge sees
    # an identical entry (no conflict) and re-migrates the repeat cleanly.
    legacy_key = _legacy("htdemucs", STABLE, "drums.flac")
    store = DictStore({legacy_key: BODY})
    first = migrate_legacy_stems(
        store, "bucket", dry_run=False, delete_after=True, ledger_key=LEDGER_KEY
    )
    assert first.deleted == 1

    store.objects[legacy_key] = BODY  # identical content re-created
    second = migrate_legacy_stems(
        store, "bucket", dry_run=False, delete_after=True, ledger_key=LEDGER_KEY
    )
    assert second.deleted == 1
    assert legacy_key not in store.objects
    assert all("already maps to" not in failure for failure in second.failures)


def test_a_legacy_object_overwritten_mid_run_is_kept_not_deleted():
    # A retained archival uploader overwrites the SAME stems/ key AFTER phase 1
    # reads and re-keys the old bytes but BEFORE the delete. Deleting it would
    # drop the newer generation while the ledger and destination point at the
    # older bytes. The delete is refused and the newer bytes survive.
    legacy_key = _legacy("htdemucs", STABLE, "drums.flac")

    class SourceSwapsAfterFirstRead(DictStore):
        """Serve the original body once (the phase-1 read) then a replacement,
        modeling a concurrent overwrite of the legacy key mid-migration."""

        def __init__(self, initial, replacement):
            super().__init__(initial)
            self._replacement = replacement
            self._reads = 0

        def get_object(self, bucket, key):
            if key == legacy_key:
                self._reads += 1
                if self._reads > 1:
                    # The archival uploader's overwrite lands after the
                    # phase-1 read: the source now holds the newer generation.
                    self.objects[key] = self._replacement
            return super().get_object(bucket, key)

    replaced = b"newer render overwrote the key while the migration ran"
    store = SourceSwapsAfterFirstRead({legacy_key: BODY}, replaced)
    result = migrate_legacy_stems(
        store, "bucket", dry_run=False, delete_after=True, ledger_key=LEDGER_KEY
    )
    assert result.migrated == 1
    assert result.deleted == 0
    assert store.objects[legacy_key] == replaced  # newer generation survives
    assert store.objects[_asset(BODY)] == BODY  # old bytes were re-keyed
    assert any(
        "changed during the migration" in failure for failure in result.failures
    )


def test_a_cross_campaign_or_unreadable_existing_ledger_refuses_the_delete():
    legacy_key = _legacy("htdemucs", STABLE, "vocals.flac")

    # An existing ledger for another bucket under the same key must not be
    # overwritten: that ledger describes a DIFFERENT campaign, and clobbering
    # it to record this one would orphan the other campaign's deletes.
    cross = DictStore({legacy_key: BODY})
    cross.objects[LEDGER_KEY] = _ledger_for("other-bucket")
    result = migrate_legacy_stems(
        cross, "bucket", dry_run=False, delete_after=True, ledger_key=LEDGER_KEY
    )
    assert result.deleted == 0
    assert legacy_key in cross.objects
    assert len(result.failures) == 1
    assert "different layout or bucket" in result.failures[0]

    # An existing ledger that does not parse must abort too: writing over it
    # would silently discard whatever it recorded.
    corrupt = DictStore({legacy_key: BODY})
    corrupt.objects[LEDGER_KEY] = b"{not json"
    result = migrate_legacy_stems(
        corrupt, "bucket", dry_run=False, delete_after=True, ledger_key=LEDGER_KEY
    )
    assert result.deleted == 0
    assert legacy_key in corrupt.objects
    assert len(result.failures) == 1
    assert "unreadable" in result.failures[0]


def _ledger_for(bucket: str) -> bytes:
    return (
        json.dumps(
            {
                "schema_version": 1,
                "layout": REKEY_LEDGER_LAYOUT,
                "bucket": bucket,
                "entries": [],
            }
        ).encode("utf-8")
    )


def test_identical_bytes_in_two_legacy_bundles_collapse_to_one_new_object():
    store = DictStore(
        {
            _legacy("htdemucs", STABLE, "vocals.flac"): BODY,
            _legacy("htdemucs", "cd34" * 10, "vocals.flac"): BODY,
        }
    )
    result = migrate_legacy_stems(store, "bucket", dry_run=False)
    assert result.migrated == 2
    assert sum(1 for k in store.objects if k.startswith("assets/")) == 1


def test_a_malformed_stems_key_in_the_scan_is_reported_not_guessed():
    # Two-segment "stems/htdemucs" is under the stems/ prefix but is not a
    # four-segment bundle-object key; it must be reported, never re-keyed by
    # guessing a filename.
    store = DictStore({"stems/htdemucs": BODY})
    result = migrate_legacy_stems(store, "bucket", dry_run=True)
    assert result.migrated == 0
    assert len(result.failures) == 1
    assert "not a legacy stems key" in result.failures[0]


def test_a_key_forced_into_the_scan_that_is_not_legacy_is_reported():
    # A caller scanning a broad prefix or passing a keys override can surface
    # an assets/... key; it must be reported, never silently moved.
    result = migrate_legacy_stems(
        DictStore(), "bucket", dry_run=True,
        keys=["assets/ab/" + "ab" * 32],
    )
    assert result.migrated == 0
    assert len(result.failures) == 1
    assert "not a legacy stems key" in result.failures[0]


def test_rekey_plan_is_pure_and_reports_missing_bodies():
    legacy_key = _legacy("htdemucs", STABLE, "vocals.flac")
    plan = rekey_plan([legacy_key, _legacy("htdemucs", STABLE, "bass.flac")],
                      {legacy_key: BODY})
    assert plan.migrated == 1
    assert plan.rekeyed[0] == (legacy_key, _asset(BODY))
    assert len(plan.failures) == 1
    assert "no body supplied" in plan.failures[0]
