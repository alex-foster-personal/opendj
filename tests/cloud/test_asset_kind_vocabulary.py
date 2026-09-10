"""The asset-kind vocabulary is ONE vocabulary, pinned across all five sites.

Main shipped ``lyrics_cache`` in ``apps.cloud.policy.ARTIFACT_KINDS`` while
``hydration_core.ASSET_KINDS`` and the ``sync_policies.asset_kind`` CHECK still
refused it (issue #1470). Nothing caught that, because each site was only ever
compared against itself. This module is the tripwire: registering a kind in one
place and forgetting another is now a red test, not a runtime surprise in a
venue.

The five sites, in the order a new kind travels through them:
  1. ``apps.shared.state.migrations_v10.ASSET_KIND_CHECK_VALUES`` -- the DDL CHECK
  2. ``apps.cloud.hydration_core.ASSET_KINDS``                   -- the runtime gate
  3. ``apps.cloud.policy.ARTIFACT_KINDS``                        -- the storage policy
  4. ``routes.cloudsync.AssetKind``                              -- the HTTP contract
  5. ``src/lib/api-cloudsync.ts`` ``ASSET_KINDS``                -- the UI contract

Site 5 is hand-maintained TypeScript, so it is read as text rather than
imported. ``src/lib/api-types.ts`` and ``apps/webui/openapi.json`` are
GENERATED from site 4 and are checked by ``just openapi-dump`` /
``pnpm run api:gen`` in the pre-push recipe, not here.

Regression one-liners:
  - if a new asset kind lands in policy.py but not the _V10 CHECK then broken
  - if a new asset kind lands in the CHECK but not hydration_core.ASSET_KINDS then broken
  - if the routes AssetKind Literal drifts from the Python vocabulary then broken
  - if api-cloudsync.ts ASSET_KINDS drifts from the Python vocabulary then broken
  - if the TS ASSET_KINDS array cannot be parsed out of the source then broken
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest

from apps.cloud import hydration_core, policy
from apps.shared.platform_paths import PROJECT_ROOT
from apps.shared.state import migrations_v10
from apps.webui.server.routes import cloudsync as cloudsync_routes

_TS_MODULE: Path = (
    PROJECT_ROOT / "apps" / "webui" / "frontend" / "src" / "lib" / "api-cloudsync.ts"
)
_TS_ASSET_KINDS = re.compile(r"export const ASSET_KINDS = \[(?P<body>.*?)\] as const;", re.DOTALL)


def _typescript_asset_kinds() -> tuple[str, ...]:
    """Read the hand-maintained ``ASSET_KINDS`` array out of the TS module."""
    source = _TS_MODULE.read_text(encoding="utf-8")
    match = _TS_ASSET_KINDS.search(source)
    if match is None:
        raise AssertionError(
            f"could not find `export const ASSET_KINDS = [...] as const;` in {_TS_MODULE}"
        )
    return tuple(re.findall(r"'([^']+)'", match.group("body")))


@pytest.mark.requirement("CLOUDSYNC-01")
def test_every_asset_kind_registry_holds_the_same_vocabulary() -> None:
    """[if] one registry gains a kind the others lack [then] this fails, [else stop]."""
    check_values = set(migrations_v10.ASSET_KIND_CHECK_VALUES)
    assert set(hydration_core.ASSET_KINDS) == check_values
    assert set(policy.ARTIFACT_KINDS) == check_values
    assert set(get_args(cloudsync_routes.AssetKind)) == check_values
    assert set(_typescript_asset_kinds()) == check_values


@pytest.mark.requirement("CLOUDSYNC-01")
def test_karaoke_words_is_registered_everywhere() -> None:
    """[if] the words artifact kind is missing from any registry [then] fail, [else stop]."""
    assert "karaoke_words" in migrations_v10.ASSET_KIND_CHECK_VALUES
    assert "karaoke_words" in hydration_core.ASSET_KINDS
    assert "karaoke_words" in policy.ARTIFACT_KINDS
    assert "karaoke_words" in get_args(cloudsync_routes.AssetKind)
    assert "karaoke_words" in _typescript_asset_kinds()


@pytest.mark.requirement("CLOUDSYNC-01")
def test_lyrics_cache_is_registered_everywhere() -> None:
    """[if] the #1470 lyrics_cache gap reopens anywhere [then] fail, [else stop]."""
    assert "lyrics_cache" in migrations_v10.ASSET_KIND_CHECK_VALUES
    assert "lyrics_cache" in hydration_core.ASSET_KINDS
    assert "lyrics_cache" in policy.ARTIFACT_KINDS
    assert "lyrics_cache" in get_args(cloudsync_routes.AssetKind)
    assert "lyrics_cache" in _typescript_asset_kinds()


@pytest.mark.requirement("CLOUDSYNC-01")
def test_no_registry_carries_a_duplicate_kind() -> None:
    """[if] a registry lists a kind twice [then] fail, [else stop]."""
    for name, kinds in (
        ("migrations_v10.ASSET_KIND_CHECK_VALUES", migrations_v10.ASSET_KIND_CHECK_VALUES),
        ("hydration_core.ASSET_KINDS", hydration_core.ASSET_KINDS),
        ("policy.ARTIFACT_KINDS", policy.ARTIFACT_KINDS),
        ("routes.cloudsync.AssetKind", get_args(cloudsync_routes.AssetKind)),
        ("api-cloudsync.ts ASSET_KINDS", _typescript_asset_kinds()),
    ):
        assert len(set(kinds)) == len(kinds), f"{name} lists a kind twice: {kinds}"


@pytest.mark.requirement("CLOUDSYNC-01")
def test_every_registered_kind_has_a_storage_layout_and_cloud_defaults() -> None:
    """[if] a kind is registered without a layout or defaults [then] fail, [else stop]."""
    for kind in policy.ARTIFACT_KINDS:
        local_cache_path, cache_budget_mb = policy._ARTIFACT_LAYOUT[kind]
        assert local_cache_path.startswith("state/")
        assert cache_budget_mb > 0
        assert set(policy._CLOUD_DEFAULTS[kind]) == set(policy.MACHINE_CLASSES)


@pytest.mark.requirement("CLOUDSYNC-01")
def test_karaoke_words_layout_matches_the_pr2_contract() -> None:
    """[if] the words cache path or budget drifts from D13.2 [then] fail, [else stop]."""
    local_cache_path, cache_budget_mb = policy._ARTIFACT_LAYOUT["karaoke_words"]
    assert local_cache_path == "state/karaoke-cache/{stable_id}.json"
    assert cache_budget_mb == 512
    assert set(policy._CLOUD_DEFAULTS["karaoke_words"].values()) == {"pinned"}
