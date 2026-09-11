"""The ONE R2 object key layout for every producer (cloudsync, issue #1452).

Cloudsync part 3 unified two layouts that the merge-lane audit found side by
side in this repo (``specs/cloudsync-spec.md``, ADR 06):

* asset tier -- content-addressed ``assets/<sha256[:2]>/<sha256>``, where the
  key is the SHA-256 of the object body;
* stems farm -- path-addressed ``stems/<preset>/<stable_id>/<filename>``.

Decision: content addressing. Every object a producer writes to R2 is keyed
by the digest of its own body, under the asset-tier layout. This is what the
spec asks for (HQ stems "content-addressed by content_hash", cloudsync-spec
section 0.2 / D4) and what the policy object names as its target
(``R2_CONTENT_ADDRESSED_KEY`` in ``apps/cloud/policy.py``, issue #1450):
identical bytes deduplicate to one object and every fetch can verify the
body it received against its key.

What is traded away: path addressing's index-free, human-navigable
discovery -- under ``stems/<preset>/<stable_id>/`` an operator can browse a
bucket and find a preset's output without any index. Under content
addressing a bucket listing of ``assets/`` is a flat wall of shards; the
index that replaces the path is the metadata sidecar row (ADR 06 point 1,
``track_locations`` kind='remote'), which the policy engine writes. The
mapping for objects already written in the losing layout lives here too
(``parse_legacy_stem_key`` / ``rekey_legacy_stem_key``).

This module is the single import point for the layout. The asset tier's own
derivation in :mod:`apps.cloud.asset_store` is re-exported here, never
copied, so the two cannot drift.
"""
from __future__ import annotations

import dataclasses

from .asset_store import ASSET_PREFIX, HASH_SHARD_LEN, SHA256_HEX_LEN, asset_object_key

#: The one content-addressed layout, in template form. Every producer derives
#: keys through :func:`asset_object_key`, which realizes exactly this string.
R2_KEY_TEMPLATE: str = f"{ASSET_PREFIX}/{{sha256[:{HASH_SHARD_LEN}]}}/{{sha256}}"

#: The pre-unification path-addressed stems layout. Kept so the migration can
#: recognize, parse and re-key objects already written in it. Nothing NEW is
#: written here.
LEGACY_STEMS_TEMPLATE: str = "stems/{preset}/{stable_id}/{filename}"

#: Prefix ``apps.shared.hashing`` puts on stored content hashes
#: (``sha256:<64-hex>``). The state layer stores it; R2 keys need the bare
#: hex, so the stored form must be normalized at the DB -> key boundary.
HASH_PREFIX: str = "sha256:"

_HEX_DIGITS: str = "0123456789abcdef"


class R2KeyError(ValueError):
    """A value cannot name an R2 object key under the canonical layout."""


@dataclasses.dataclass(frozen=True)
class LegacyStemKey:
    """The three path segments a legacy ``stems/...`` key encodes."""

    preset: str
    stable_id: str
    filename: str


# --- stored-digest normalization --------------------------------------------


def canonical_digest(content_hash: str) -> str:
    """Return the bare lowercase 64-hex digest for a stored ``content_hash``.

    Accepts both spellings found in the codebase: the bare hex the asset tier
    computes and stores, and the ``sha256:<hex>`` prefixed form
    ``apps.shared.hashing`` writes to ``tracks.content_hash``. Anything else
    (uppercase, truncated, non-hex, a second unknown prefix) raises rather
    than minting a key no producer could reproduce.
    """
    candidate = content_hash.strip()
    if candidate.startswith(HASH_PREFIX):
        candidate = candidate[len(HASH_PREFIX) :]
    if (
        len(candidate) != SHA256_HEX_LEN
        or ":" in candidate
        or any(char not in _HEX_DIGITS for char in candidate)
    ):
        raise R2KeyError(
            f"content_hash must be {SHA256_HEX_LEN} lowercase hex chars "
            f"(optionally prefixed '{HASH_PREFIX}'): got {content_hash!r}"
        )
    return candidate


def object_key_for_stored_digest(content_hash: str) -> str:
    """Content-addressed key for a STORED hash, in either spelling.

    ``tracks.content_hash`` carries the ``sha256:`` prefix, so the backfilled
    values cannot address objects until normalized. This is that boundary:
    normalize, then derive the key.
    """
    return asset_object_key(canonical_digest(content_hash))


# --- legacy layout: recognize + map to the canonical key ---------------------


def legacy_stem_key(preset: str, stable_id: str, filename: str) -> str:
    """Synthesize the pre-unification path-addressed key for a bundle object.

    Migration-facing only: nothing new should be written under this layout.
    """
    if not preset or not stable_id or not filename:
        raise R2KeyError(
            f"legacy stem key needs non-empty preset, stable_id, filename; "
            f"got {preset!r} / {stable_id!r} / {filename!r}"
        )
    return f"stems/{preset}/{stable_id}/{filename}"


def parse_legacy_stem_key(key: str) -> LegacyStemKey:
    """Split a legacy ``stems/<preset>/<stable_id>/<filename>`` key.

    Raises :class:`R2KeyError` for anything that is not that exact shape, so
    a migration pass can never silently skip or mis-file an object.
    """
    parts = key.split("/")
    if (
        len(parts) != 4
        or parts[0] != "stems"
        or not parts[1]
        or not parts[2]
        or not parts[3]
    ):
        raise R2KeyError(
            f"not a legacy stems/<preset>/<stable_id>/<filename> key: {key!r}"
        )
    return LegacyStemKey(preset=parts[1], stable_id=parts[2], filename=parts[3])


def rekey_legacy_stem_key(legacy_key: str, content_sha256: str) -> str:
    """Map a legacy key to the content-addressed key of the SAME object.

    A content address is a property of the object body, never of its old key,
    so the digest must be read from the bytes before the mapping exists. This
    function validates that the source is a legacy stems key and returns where
    that object lives under the canonical layout -- the migration's old -> new
    pair for one object.
    """
    parse_legacy_stem_key(legacy_key)
    return asset_object_key(content_sha256)


__all__ = [
    "ASSET_PREFIX",
    "HASH_PREFIX",
    "HASH_SHARD_LEN",
    "LEGACY_STEMS_TEMPLATE",
    "R2_KEY_TEMPLATE",
    "SHA256_HEX_LEN",
    "LegacyStemKey",
    "R2KeyError",
    "asset_object_key",
    "canonical_digest",
    "legacy_stem_key",
    "object_key_for_stored_digest",
    "parse_legacy_stem_key",
    "rekey_legacy_stem_key",
]
