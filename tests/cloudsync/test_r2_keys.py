"""Canonical R2 object key layout: ONE layout, both producers, migration map.

Cloudsync issue #1452 (part 3 of 4) unified two divergent R2 key layouts:

* asset tier -- content-addressed ``assets/<sha256[:2]>/<sha256>`` (ADR 06);
* stems farm -- path-addressed ``stems/<preset>/<stable_id>/<filename>``.

The decision, recorded in ``apps/cloud/r2_keys.py`` and asserted here, is
that every R2 object a producer writes is keyed by the SHA-256 of its own
body under the content-addressed layout. What is traded away is path
addressing's index-free, human-navigable discovery (browse ``stems/<preset>``
to find a bundle without any index); the replacement index is the metadata
sidecar row in ``track_locations`` (ADR 06 point 1), which the policy engine
(issue #1450) writes. Acceptances under test:

* key derivation -- any producer, given the same bytes, derives the same key;
* stored-digest normalization -- ``tracks.content_hash`` is stored with a
  ``sha256:`` prefix by ``apps.shared.hashing``, while the R2 key needs the
  bare 64-hex digest, so a stored digest must resolve to a key or content
  addressing stays blocked;
* old -> new mapping -- a legacy ``stems/<preset>/<stable_id>/<filename>``
  key maps onto the content-addressed key of the SAME object.
"""
from __future__ import annotations

import hashlib

import pytest

from apps.cloud import r2_keys
from apps.cloud.r2_keys import R2KeyError


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


# --- canonical content-addressed key ----------------------------------------


def test_content_addressed_key_is_sharded_and_deterministic():
    digest = _sha(b"stem bundle bytes")
    key = r2_keys.asset_object_key(digest)
    assert key == f"assets/{digest[:2]}/{digest}"
    assert r2_keys.asset_object_key(digest) == key


def test_the_template_matches_the_asset_tier_implementation():
    # The single layout must be the SAME string the asset tier mints from,
    # or the "one layout" claim is decoration. r2_keys re-exports the asset
    # tier derivation, so this is a structural identity, not a copy.
    assert r2_keys.asset_object_key(_sha(b"x")).startswith("assets/")
    assert r2_keys.R2_KEY_TEMPLATE == "assets/{sha256[:2]}/{sha256}"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "  ",
        "abc",
        _sha(b"x").upper(),  # uppercase hex is a different string on S3
        _sha(b"x")[:-1],  # truncated
        _sha(b"x")[:-1] + "z",  # non-hex char
        "sha256:" + _sha(b"x")[:-1],  # prefixed but truncated
    ],
)
def test_canonical_digest_refuses_anything_that_is_not_a_sha256(bad: str):
    with pytest.raises(R2KeyError):
        r2_keys.canonical_digest(bad)


# --- stored-digest normalization ---------------------------------------------


def test_canonical_digest_accepts_the_bare_form():
    digest = _sha(b"body")
    assert r2_keys.canonical_digest(digest) == digest


def test_canonical_digest_strips_the_state_layer_prefix():
    # apps.shared.hashing stores "sha256:<64-hex>"; the R2 key needs the
    # bare hex. Both spellings must resolve to the same key or backfilled
    # content_hash values still cannot address objects.
    digest = _sha(b"body")
    assert r2_keys.canonical_digest(f"sha256:{digest}") == digest


def test_stored_digest_and_bare_digest_mint_the_same_key():
    digest = _sha(b"audio bytes")
    assert r2_keys.object_key_for_stored_digest(f"sha256:{digest}") == (
        r2_keys.object_key_for_stored_digest(digest)
    )


# --- old -> new mapping for the losing layout --------------------------------


def test_legacy_stem_key_round_trips_through_the_parser():
    legacy = r2_keys.legacy_stem_key("htdemucs", "ab12" * 10, "vocals.flac")
    assert legacy == "stems/htdemucs/ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12/vocals.flac"
    ref = r2_keys.parse_legacy_stem_key(legacy)
    assert ref.preset == "htdemucs"
    assert ref.stable_id == "ab12" * 10
    assert ref.filename == "vocals.flac"


def test_parse_legacy_stem_key_refuses_other_shapes():
    for bad in [
        "assets/ab/ab12",
        "stems/htdemucs/ab12",  # too few segments (no filename)
        "stems//ab12/vocals.flac",  # empty preset
        "",
    ]:
        with pytest.raises(R2KeyError):
            r2_keys.parse_legacy_stem_key(bad)


def test_rekey_maps_a_legacy_key_to_the_content_addressed_key_of_its_body():
    # Content addresses cannot be derived from a path key -- the digest must
    # come from reading the object body. The mapping is old key -> new key
    # for the SAME bytes, which is what makes it a migration and not a copy.
    legacy = r2_keys.legacy_stem_key("htdemucs", "ab12" * 10, "vocals.flac")
    body = b"some rendered vocals"
    assert r2_keys.rekey_legacy_stem_key(legacy, _sha(body)) == (
        r2_keys.asset_object_key(_sha(body))
    )


def test_rekey_is_idempotent_for_identical_bytes():
    legacy = r2_keys.legacy_stem_key("htdemucs", "ab12" * 10, "vocals.flac")
    digest = _sha(b"identical bytes")
    assert r2_keys.rekey_legacy_stem_key(legacy, digest) == (
        r2_keys.rekey_legacy_stem_key(legacy, digest)
    )
