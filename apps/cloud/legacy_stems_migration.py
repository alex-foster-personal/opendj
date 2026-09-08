"""Migration: re-key already-written ``stems/<preset>/<stable_id>/<file>``
objects onto the ONE content-addressed layout (cloudsync issue #1452).

The repo used to hold two R2 layouts side by side: the asset tier wrote
``assets/<sha256[:2]>/<sha256>`` and the stems farm wrote path-addressed
``stems/<preset>/<stable_id>/<file>``. Content addressing won; objects
already written in the losing layout still sit under ``stems/`` keys until
someone moves them. This module IS that move: list legacy keys, read each
body, derive the content address (which is a property of the body, never of
the old key), write the object to its new key, verify the new copy reads
back byte-identical, and only then delete the legacy key.

Three properties make the destructive half safe:

* Nothing is deleted unless ``delete_after=True`` AND the object written
  under the new key verifies byte-identical (a short or truncated PUT is the
  one irreversible failure mode, so it is checked before the ledger is even
  written) AND the legacy key still holds those same bytes at delete time (a
  concurrent overwrite by a retained archival uploader keeps the newer
  generation and reports instead of deleting under a stale mapping).
* With ``delete_after`` the durable rekey ledger is written to the bucket
  only after destination verification, so it records only mappings whose new
  object verifies: a corrupt new object must not be advertised as migrated to
  a later sidecar backfill or ledger consumer. Content-addressed keys are
  opaque: they carry no preset, stable_id or filename, so once the legacy
  ``stems/...`` key is gone the mapping is the only record of which bundle an
  object belongs to. The ledger object (under ``ledger_key``) records every
  verified old -> new pair plus the parsed preset/stable_id/filename and is
  verified to read back before the first delete; a failed ledger write aborts
  the run with zero deletions.
* Reusing a ledger key across ``--limit`` batches EXTENDS it (entries union
  by old_key), so a later batch cannot overwrite an earlier batch's mapping;
  a ledger key under the scanned prefix is REFUSED so the ledger cannot be
  re-scanned and deleted by the next run; and an old_key that reappears
  mapping to DIFFERENT content is REFUSED as a conflict rather than deleting
  the legacy key under a stale ledger.

Live migration requires an enabled R2 account and credentials. No public
end-to-end acceptance is established by the private account history.
The logic is written against a narrow object-store protocol
(:class:`LegacyStemsObjectStore`) and driven by ``scripts/rekey_stems_layout.py``
with a boto3 R2 adapter; the same logic is exercised here against a
dict-backed store so the ORDER (ledger -> write-new -> verify -> delete-old)
is what a test proves, not a mock's cooperation.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Protocol

from .r2_keys import (
    LegacyStemKey,
    R2KeyError,
    parse_legacy_stem_key,
    rekey_legacy_stem_key,
)

LEGACY_STEMS_PREFIX: str = "stems/"

#: Layout name stamped into every rekey ledger object, so a later reader (or
#: the DB sidecar backfill, issue #1450) can tell what a ledger describes.
REKEY_LEDGER_LAYOUT: str = "legacy-stems->content-addressed"


class LegacyStemsObjectStore(Protocol):
    """The narrow surface a migration needs from the target object store."""

    def list_objects(self, bucket: str, prefix: str) -> list[str]:
        """Every object key under ``prefix`` (paginated by the adapter)."""

    def get_object(self, bucket: str, key: str) -> bytes | None:
        """Body for ``key``, or ``None`` if it does not exist."""

    def put_object(self, bucket: str, key: str, body: bytes) -> None:
        """Write ``body`` to ``key`` unconditionally."""

    def delete_object(self, bucket: str, key: str) -> None:
        """Delete ``key``."""


@dataclasses.dataclass(frozen=True)
class RekeyResult:
    """Outcome of one :func:`migrate_legacy_stems` invocation."""

    migrated: int = 0
    deleted: int = 0
    failures: tuple[str, ...] = ()
    #: old key -> new key, one entry per migrated object. This is the audit
    #: trail: a re-run with the same store is a no-op because the old keys no
    #: longer exist (or already map to an object present under the new key).
    rekeyed: tuple[tuple[str, str], ...] = ()
    #: The ledger key written when ``delete_after`` was honored, or ``None``.
    ledger_key: str | None = None


def _sha256_filelike(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _object_holds_digest(
    store: LegacyStemsObjectStore, bucket: str, new_key: str, digest: str
) -> bool:
    """The object under ``new_key`` must contain exactly the re-keyed body.

    ``digest`` is the content address embedded in ``new_key`` (the sha256 the
    body was re-keyed under). Presence is not proof; bytes are: read the new
    object and confirm its hash equals the address, so a short/truncated PUT
    (which stores fewer bytes than the address promises) fails the check.
    """
    got = store.get_object(bucket, new_key)
    return got is not None and _sha256_filelike(got) == digest


def _ledger_body(
    entries: list[dict[str, str]], bucket: str
) -> bytes:
    """Serialize one rekey ledger object.

    Every migrated old -> new pair plus the parsed preset/stable_id/filename,
    so the mapping survives the deletion of the legacy keys that encoded it.
    """
    ledger = {
        "schema_version": 1,
        "layout": REKEY_LEDGER_LAYOUT,
        "bucket": bucket,
        "entries": entries,
    }
    return (json.dumps(ledger, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _validate_delete_after_cfg(
    delete_after: bool, dry_run: bool, ledger_key: str | None, prefix: str
) -> None:
    """Refuse a delete_after run that cannot be made safe.

    Two preconditions: a live destructive run needs a ledger key (without it
    the delete drops the only bundle mapping), and that ledger must live
    OUTSIDE the whole legacy namespace, not merely outside this run's
    (possibly narrowed) scan prefix. A ledger under ``stems/`` - even one a
    narrowed ``--prefix stems/htdemucs/`` batch wrote elsewhere in the legacy
    namespace - would be re-scanned as a legacy object and deleted by a later
    default run, so the index recording what the migration deleted would be
    the next migration's casualty. Refusing loudly beats a destructive
    default.
    """
    if delete_after and not dry_run and not ledger_key:
        raise ValueError(
            "delete_after requires ledger_key so the old -> new mapping is "
            "durably recorded before the legacy keys are removed"
        )
    if (
        delete_after
        and not dry_run
        and ledger_key is not None
        and ledger_key.startswith((prefix, LEGACY_STEMS_PREFIX))
    ):
        raise ValueError(
            f"ledger_key {ledger_key!r} lies under the legacy stems namespace "
            f"({LEGACY_STEMS_PREFIX!r}): the ledger records the mapping the "
            "migration deletes, so a later run would re-scan and delete it. "
            "Use a key outside the namespace, e.g. _ledgers/<name>.json"
        )


def _scan_keys(
    store: LegacyStemsObjectStore,
    bucket: str,
    prefix: str,
    keys: list[str] | None,
    ledger_key: str | None,
) -> list[str]:
    """The objects a run will migrate, never including its own ledger.

    ``keys`` overrides the store listing (a ``--limit`` batch), but whichever
    source supplies the scan, the ledger object itself is metadata and must
    not be migrated or deleted by the run that maintains it.
    """
    listed = store.list_objects(bucket, prefix) if keys is None else keys
    if ledger_key is None:
        return listed
    return [key for key in listed if key != ledger_key]


def migrate_legacy_stems(
    store: LegacyStemsObjectStore,
    bucket: str,
    *,
    dry_run: bool,
    delete_after: bool = False,
    prefix: str = LEGACY_STEMS_PREFIX,
    keys: list[str] | None = None,
    ledger_key: str | None = None,
) -> RekeyResult:
    """Re-key every legacy ``stems/...`` object under ``bucket``.

    ``dry_run=True`` reads every body (so the plan is honest: it hashes the
    real bytes) but writes and deletes nothing. ``keys`` overrides the store
    listing (a ``--limit`` batch is still a genuine subset, not a different
    algorithm).

    ``delete_after=True`` removes each legacy key only after its new object
    verifies byte-identical, only after a durable ledger recording the
    VERIFIED old -> new mapping was written and read back, and only while the
    legacy key still holds those same bytes (a concurrent overwrite keeps the
    newer generation and reports instead of deleting). A live (non-dry-run)
    delete_after run therefore REQUIRES ``ledger_key`` and refuses one that
    lies under the scanned prefix; the refusals and verifications beat a
    destructive default that silently drops the bundle association.
    """
    _validate_delete_after_cfg(delete_after, dry_run, ledger_key, prefix)
    scan = _scan_keys(store, bucket, prefix, keys, ledger_key)
    ledger_entries: list[dict[str, str]] = []
    migrated: list[tuple[str, str]] = []
    failures: list[str] = []

    # Phase 1: parse, read, re-key, write the new object. Nothing is deleted
    # in this phase, so a failure here never destroys the only copy.
    for old_key in scan:
        try:
            parsed: LegacyStemKey = parse_legacy_stem_key(old_key)
        except R2KeyError as exc:
            failures.append(f"{old_key}: not a legacy stems key: {exc}")
            continue
        body = store.get_object(bucket, old_key)
        if body is None:
            failures.append(f"{old_key}: read returned no body")
            continue
        digest = _sha256_filelike(body)
        new_key = rekey_legacy_stem_key(old_key, digest)
        migrated.append((old_key, new_key))
        ledger_entries.append(
            {
                "old_key": old_key,
                "new_key": new_key,
                "preset": parsed.preset,
                "stable_id": parsed.stable_id,
                "filename": parsed.filename,
            }
        )
        if not dry_run:
            store.put_object(bucket, new_key, body)

    if dry_run or not delete_after:
        return RekeyResult(
            migrated=len(migrated),
            failures=tuple(failures),
            rekeyed=tuple(migrated),
        )

    assert ledger_key is not None  # _validate_delete_after_cfg already rejected it
    return _commit_delete_after(
        store, bucket, ledger_key, ledger_entries, migrated, failures
    )


def _merge_into_prior_ledger(
    existing: bytes | None,
    new_entries: list[dict[str, str]],
    bucket: str,
    ledger_key: str,
) -> tuple[list[dict[str, str]] | None, str | None]:
    """Union new batch entries into any compatible ledger at ``ledger_key``.

    A ``--delete-after --limit`` campaign can span several batches reusing one
    ledger key. Entries only ever ACCUMULATE (unioned by ``old_key``), so a
    later batch cannot erase an earlier batch's mapping the way an
    unconditional overwrite would. Returns the entries to write and ``None``;
    on an unreadable, cross-campaign or CONFLICTING existing ledger returns
    ``(None, reason)`` so the caller aborts deletes - proceeding would be the
    exact loss this guards against.
    """
    if existing is None:
        return new_entries, None
    try:
        prior = json.loads(existing.decode("utf-8"))
        prior_entries = prior["entries"]
    except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
        return None, f"{ledger_key}: existing ledger unreadable ({exc}); delete aborted"
    if (
        prior.get("schema_version") != 1
        or prior.get("layout") != REKEY_LEDGER_LAYOUT
        or prior.get("bucket") != bucket
        or not isinstance(prior_entries, list)
    ):
        return None, (
            f"{ledger_key}: existing ledger is for a different layout or "
            "bucket; use a fresh ledger key per campaign; delete aborted"
        )
    return _merge_without_conflicts(prior_entries, new_entries, ledger_key)


def _merge_without_conflicts(
    prior_entries: list[dict[str, str]],
    new_entries: list[dict[str, str]],
    ledger_key: str,
) -> tuple[list[dict[str, str]] | None, str | None]:
    """Union new batch entries into ``prior_entries``, refusing a conflict.

    A repeated old_key is the idempotent re-run case ONLY when the new entry
    is identical (the legacy object unchanged). If the same old_key now maps
    to a DIFFERENT new_key, the legacy object was replaced between runs and
    the prior ledger points at stale content: deleting the legacy key under
    that stale mapping would permanently lose the bundle association, so the
    merge REFUSES and the caller aborts the delete. Entries only ever
    accumulate otherwise.
    """
    prior_by_old = {entry["old_key"]: entry for entry in prior_entries}
    additions: list[dict[str, str]] = []
    for entry in new_entries:
        prior_entry = prior_by_old.get(entry["old_key"])
        if prior_entry is None:
            additions.append(entry)
        elif prior_entry != entry:
            return None, (
                f"{ledger_key}: {entry['old_key']} already maps to "
                f"{prior_entry['new_key']} but this run derived "
                f"{entry['new_key']}; the legacy object changed between "
                "runs. Delete aborted - record the new content under a "
                "fresh ledger key."
            )
    return prior_entries + additions, None


def _verified_rekeys(
    store: LegacyStemsObjectStore,
    bucket: str,
    migrated: list[tuple[str, str]],
    ledger_entries: list[dict[str, str]],
    failures: list[str],
) -> list[tuple[str, str, dict[str, str]]]:
    """The destination-verified (old_key, new_key, entry) triples; the rest fail.

    A content-addressed destination whose object does not hash to the digest
    its key advertises (a short or truncated PUT) must not enter the durable
    ledger: recording it would let a later sidecar backfill or ledger consumer
    publish an object that breaks the address's own promise. Verification runs
    HERE, before the ledger is written, so only verified mappings are ledgered
    and later deleted against.
    """
    verified: list[tuple[str, str, dict[str, str]]] = []
    for (old_key, new_key), entry in zip(migrated, ledger_entries, strict=True):
        digest = new_key.rsplit("/", 1)[-1]
        if _object_holds_digest(store, bucket, new_key, digest):
            verified.append((old_key, new_key, entry))
        else:
            failures.append(
                f"{old_key}: new object {new_key} failed byte-identical "
                "verification; legacy key kept"
            )
    return verified


def _delete_source_verified_rekeys(
    store: LegacyStemsObjectStore,
    bucket: str,
    verified: list[tuple[str, str, dict[str, str]]],
    failures: list[str],
) -> int:
    """Delete each migrated legacy key whose source STILL holds the migrated bytes.

    Phase 1 read and hashed the legacy object, then the run re-keyed those
    bytes. If a retained archival uploader overwrites the same ``stems/...``
    key between that read and this delete, deleting it would drop the NEWER
    generation while the ledger and destination point at the older bytes. Each
    legacy key is therefore re-read immediately before its delete and removed
    only if it still hashes to the migrated digest; a changed source is kept
    and reported so the replacement wins over the migration. The read-to-delete
    gap is the narrowest this narrow protocol allows; a versioned conditional
    delete (R2 If-Match) is the atomic form should that residual ever matter.
    """
    deleted = 0
    for old_key, new_key, _entry in verified:
        digest = new_key.rsplit("/", 1)[-1]
        if _object_holds_digest(store, bucket, old_key, digest):
            store.delete_object(bucket, old_key)
            deleted += 1
        else:
            failures.append(
                f"{old_key}: legacy object changed during the migration (no "
                "longer the bytes this run re-keyed); legacy key kept so the "
                "newer generation is not dropped"
            )
    return deleted


def _commit_delete_after(
    store: LegacyStemsObjectStore,
    bucket: str,
    ledger_key: str,
    ledger_entries: list[dict[str, str]],
    migrated: list[tuple[str, str]],
    failures: list[str],
) -> RekeyResult:
    """Phase 2 of a live ``delete_after`` run: verify, ledger, delete.

    Destination verification runs FIRST, so only verified mappings enter the
    durable ledger (a corrupt destination is reported, never advertised as
    migrated). The ledger is then written and verified to read back
    byte-identical BEFORE any delete, and each legacy key is deleted only while
    its source still holds the migrated bytes. A ledger that did not persist
    aborts with no deletes. A prior ledger at ``ledger_key`` is extended,
    never overwritten (a ``--delete-after --limit`` campaign may span several
    batches against one key). Kept as its own function so the public entry
    point stays below the repo's cyclomatic ceiling.
    """
    verified = _verified_rekeys(store, bucket, migrated, ledger_entries, failures)
    if not verified:
        return RekeyResult(
            migrated=len(migrated),
            failures=tuple(failures),
            rekeyed=tuple(migrated),
        )

    entries = [entry for _old_key, _new_key, entry in verified]
    existing = store.get_object(bucket, ledger_key)
    merged, reason = _merge_into_prior_ledger(
        existing, entries, bucket, ledger_key
    )
    if reason is not None:
        failures.append(reason)
        return RekeyResult(
            migrated=len(migrated),
            failures=tuple(failures),
            rekeyed=tuple(migrated),
        )
    assert merged is not None  # reason None means a compatible merge succeeded

    ledger_bytes = _ledger_body(merged, bucket)
    store.put_object(bucket, ledger_key, ledger_bytes)
    if store.get_object(bucket, ledger_key) != ledger_bytes:
        # The failed overwrite may have just clobbered a prior batch's durable
        # ledger at this key: those legacy keys are ALREADY deleted, so their
        # old -> new mapping has no other record and reporting this batch's
        # failure does not bring it back. Restore the verified prior body (best
        # effort - a store that truncates every put already failed batch 1 and
        # left nothing durable to restore).
        if existing is not None:
            store.put_object(bucket, ledger_key, existing)
        failures.append(
            f"{ledger_key}: rekey ledger did not persist byte-identical; "
            "nothing was deleted"
            + ("" if existing is None else "; prior ledger restore attempted")
        )
        return RekeyResult(
            migrated=len(migrated),
            failures=tuple(failures),
            rekeyed=tuple(migrated),
        )

    deleted = _delete_source_verified_rekeys(store, bucket, verified, failures)
    return RekeyResult(
        migrated=len(migrated),
        deleted=deleted,
        failures=tuple(failures),
        rekeyed=tuple(migrated),
        ledger_key=ledger_key,
    )


def rekey_plan(keys: list[str], body_for_key: dict[str, bytes]) -> RekeyResult:
    """Pure old -> new plan for a set of legacy keys and their bodies.

    Exposed for tests and for a dry-run report without a store: content
    addresses are a property of the body, so the mapping cannot be computed
    from the key alone.
    """
    migrated: list[tuple[str, str]] = []
    failures: list[str] = []
    for old_key in keys:
        try:
            parse_legacy_stem_key(old_key)
        except R2KeyError as exc:
            failures.append(f"{old_key}: not a legacy stems key: {exc}")
            continue
        body = body_for_key.get(old_key)
        if body is None:
            failures.append(f"{old_key}: no body supplied for planning")
            continue
        migrated.append((old_key, rekey_legacy_stem_key(old_key, _sha256_filelike(body))))
    return RekeyResult(
        migrated=len(migrated),
        failures=tuple(failures),
        rekeyed=tuple(migrated),
    )


__all__ = [
    "LEGACY_STEMS_PREFIX",
    "REKEY_LEDGER_LAYOUT",
    "LegacyStemKey",
    "LegacyStemsObjectStore",
    "RekeyResult",
    "migrate_legacy_stems",
    "rekey_plan",
]
