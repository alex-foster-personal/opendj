"""LINKCONST-01: the B2B Link constants both the engine and the page apply come from one JSON source
(FE-logic audit #6646: GAP_PERF_KINDS, LOCKED_MS, the played share, the cursor anchors and the peer
projection cap were each copied by hand, and LOCKED_MS had a third copy in the relay).

- [if] the generated TS module differs from what the JSON renders [then] fail with the command that
  regenerates it [else the page and the engine apply different numbers]
- [if] the JSON is missing a key, has an extra one, or a value of the wrong type [then] loading
  fails naming it, never a default
- [if] an engine module that applies one of these reads it [then] it is the JSON value (each former
  copy, the relay's included, is pinned)

[if] engine and page read different Link constants [then] fail naming the drift, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apps.b2b_session import b2b_cursor, partner_audio, peer_now_playing
from apps.b2b_sync import link_continuity, link_relay
from apps.shared import link_constants

pytestmark = [pytest.mark.requirement("LINKCONST-01")]

REPO = Path(__file__).resolve().parents[2]
SOURCE: dict[str, Any] = json.loads(link_constants.SOURCE_PATH.read_text())


def test_the_generated_typescript_is_what_the_json_renders() -> None:
    written = (REPO / link_constants.GENERATED_TS_REL).read_text()
    assert written == link_constants.render_typescript(link_constants.load_constants(SOURCE)), (
        "the generated TS is stale: run `uv run python -m apps.shared.link_constants --write`"
    )


def test_every_former_copy_reads_the_json() -> None:
    assert tuple(SOURCE["gap_perf_kinds"]) == link_continuity.GAP_PERF_KINDS
    assert SOURCE["locked_ms"] == link_continuity.LOCKED_MS
    assert SOURCE["locked_ms"] == link_relay.CFG.LOCKED_MS, "the relay's own copy"
    assert SOURCE["played_share_min"] * 100 == partner_audio.PLAYED_PCT_MIN
    assert tuple(SOURCE["cursor_anchors"]) == b2b_cursor.CURSOR_ANCHORS
    assert SOURCE["peer_max_project_ms"] / 1000 == peer_now_playing.MAX_PROJECT_S


@pytest.mark.parametrize(
    ("mutate", "named"),
    [
        (lambda s: s.pop("locked_ms"), "locked_ms"),
        (lambda s: s.__setitem__("surprise", 1), "surprise"),
        (lambda s: s.__setitem__("locked_ms", "5"), "locked_ms"),
        (lambda s: s.__setitem__("locked_ms", True), "locked_ms"),
        (lambda s: s.__setitem__("cursor_anchors", ["deck-1", 2]), "cursor_anchors"),
        (lambda s: s.__setitem__("gap_perf_kinds", []), "gap_perf_kinds"),
        (lambda s: s.__setitem__("played_share_min", 1.5), "played_share_min"),
    ],
)
def test_a_malformed_source_fails_naming_the_key(mutate: Any, named: str) -> None:
    broken = json.loads(json.dumps(SOURCE))
    mutate(broken)
    with pytest.raises(ValueError, match=named):
        link_constants.load_constants(broken)


def test_the_source_loads_control() -> None:
    loaded = link_constants.load_constants(SOURCE)
    assert loaded.locked_ms == SOURCE["locked_ms"] and loaded.cursor_anchors == tuple(SOURCE["cursor_anchors"])
