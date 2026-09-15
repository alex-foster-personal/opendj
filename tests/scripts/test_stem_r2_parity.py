"""Parity: every copy of the R2 key layout resolves to the ONE app-layer truth.

Cloudsync issue #1452 chose content addressing (``assets/<sha256[:2]>/<sha256>``)
as THE R2 object layout. The asset tier owns the derivation
(``apps.cloud.r2_keys``); the stem rails and the Modal farm container cannot
always import the app layer, so they mirror the layout constant and a parity
test pins each mirror to the source so the two layouts cannot silently drift
apart again.

Acceptances:
* if ``scripts.r2_stems.content_addressed_key`` and the asset tier derive a
  different key for the same digest, two producers write the same bytes to
  different places -- broken.
* if the Modal farm's in-container layout constant differs from the app
  layer's, the farm writes a third layout the rails cannot read -- broken.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from apps.cloud import r2_keys
from apps.stems import r2_stems

REPO_ROOT = Path(__file__).resolve().parents[2]
FARM_SOURCE = REPO_ROOT / "scripts" / "modal_vocal_farm.py"


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def test_r2_stems_constant_matches_the_app_layer_template():
    assert r2_stems.R2_CONTENT_ADDRESSED_LAYOUT == r2_keys.R2_KEY_TEMPLATE


def test_stem_rail_key_derivation_matches_the_asset_tier():
    digest = _sha(b"the same rendered bytes")
    assert r2_stems.content_addressed_key(digest) == r2_keys.asset_object_key(
        digest
    )


def test_the_farm_container_mirrors_the_same_layout():
    """The Modal container cannot import apps.cloud, so it mirrors the layout
    constant; read its source and pin the mirror to the app layer."""
    source = FARM_SOURCE.read_text(encoding="utf-8")
    match = re.search(r'^R2_CONTENT_ADDRESSED_KEY: str = "([^"]+)"', source, re.M)
    assert match is not None, "farm lost its R2_CONTENT_ADDRESSED_KEY constant"
    assert match.group(1) == r2_keys.R2_KEY_TEMPLATE


def test_the_farm_asset_prefix_matches_the_app_layer():
    source = FARM_SOURCE.read_text(encoding="utf-8")
    prefix = re.search(r'^ASSET_PREFIX: str = "([^"]+)"', source, re.M)
    shard = re.search(r"^HASH_SHARD_LEN: int = (\d+)", source, re.M)
    assert prefix is not None and shard is not None
    assert prefix.group(1) == r2_keys.ASSET_PREFIX
    assert int(shard.group(1)) == r2_keys.HASH_SHARD_LEN
