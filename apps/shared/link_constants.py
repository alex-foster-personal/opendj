"""LINKCONST-01: the B2B Link constants the engine and the page both apply, from one JSON source.

FE-logic audit #6646 found five numbers and lists copied by hand between Python and TypeScript
(LOCKED_MS had a third copy in the relay). ``link_constants.json`` beside this file is the one
source: the engine reads it at import, and the page imports a module generated from it
(``GENERATED_TS_REL``), regenerated with ``python -m apps.shared.link_constants --write`` and held
to the JSON by ``tests/shared/test_link_constants.py``. The page module is generated rather than
imported as JSON because the frontend dev server serves nothing outside its own root.

- gap_perf_kinds: perf rows that count as an audible gap (B2BSLO, link-continuity).
- locked_ms: |phase error| within which a follower is locked (TEMPO-OFFSET-01, B2BSLO-02).
- played_share_min: under this share of real audio a partner stream is not playing (B2BAUDIO-22).
- cursor_anchors: the anchor ids a shared cursor may name, in lookup order (LINK cursor).
- peer_max_project_ms: never project a peer playhead further past its sample (a dead page freezes).
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

#: ``<role>.<took_seq or empty>.<engine id>``: the Link role claim form the B2B peer listener
#: allowlist accepts. Lives here, below every B2B package, so apps.b2b_pairing and
#: apps.b2b_sync both read it without importing each other (.importlinter).
CLAIM_PARAM_RE: Final[str] = r"(on_air|following)\.(0|[1-9][0-9]{0,8})?\.[A-Za-z0-9_-]{1,64}"
SOURCE_PATH: Final[Path] = Path(__file__).with_name("link_constants.json")
GENERATED_TS_REL: Final[str] = "apps/webui/frontend/src/lib/rb/link/link-constants.generated.ts"
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_KEYS: Final[frozenset[str]] = frozenset(
    {"_doc", "gap_perf_kinds", "locked_ms", "played_share_min", "cursor_anchors", "peer_max_project_ms"}
)


@dataclass(frozen=True)
class LinkConstants:
    gap_perf_kinds: tuple[str, ...]
    locked_ms: float
    played_share_min: float
    cursor_anchors: tuple[str, ...]
    peer_max_project_ms: float


#-------------------------------------------------------------------- validation


def _strings(source: dict[str, Any], key: str) -> tuple[str, ...]:
    value = source[key]
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
        raise ValueError(f"link_constants.json: {key} must be a non-empty list of strings, got {value!r}")
    return tuple(value)


def _number(source: dict[str, Any], key: str, *, low: float, high: float) -> float:
    value = source[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"link_constants.json: {key} must be a number in [{low}, {high}], got {value!r}")
    return float(value)


def load_constants(source: dict[str, Any]) -> LinkConstants:
    """The constants, or a ValueError naming the key: a missing, extra or mistyped key is never defaulted."""
    if set(source) != _KEYS:
        raise ValueError(f"link_constants.json: keys missing {sorted(_KEYS - set(source))}, unexpected {sorted(set(source) - _KEYS)}")
    return LinkConstants(
        gap_perf_kinds=_strings(source, "gap_perf_kinds"),
        locked_ms=_number(source, "locked_ms", low=0.0, high=1000.0),
        played_share_min=_number(source, "played_share_min", low=0.0, high=1.0),
        cursor_anchors=_strings(source, "cursor_anchors"),
        peer_max_project_ms=_number(source, "peer_max_project_ms", low=0.0, high=60_000.0),
    )


#-------------------------------------------------------------------- page module


def _ts_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else repr(value)


def _ts_strings(values: tuple[str, ...]) -> str:
    quoted = ("'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'" for v in values)
    return "[" + ", ".join(quoted) + "]"


def render_typescript(constants: LinkConstants) -> str:
    """The page module, byte for byte what ``--write`` puts at GENERATED_TS_REL."""
    return "\n".join([
        "// GENERATED from apps/shared/link_constants.json by `uv run python -m apps.shared.link_constants --write`.",
        "// Do not edit: change the JSON and regenerate (LINKCONST-01; tests/shared/test_link_constants.py checks it).",
        "",
        f"export const GAP_PERF_KINDS: readonly string[] = {_ts_strings(constants.gap_perf_kinds)};",
        f"export const LOCKED_MS = {_ts_number(constants.locked_ms)};",
        f"export const PLAYED_SHARE_MIN = {_ts_number(constants.played_share_min)};",
        f"export const CURSOR_ANCHORS = {_ts_strings(constants.cursor_anchors)} as const;",
        f"export const PEER_MAX_PROJECT_MS = {_ts_number(constants.peer_max_project_ms)};",
        "",
    ])


LINK: Final[LinkConstants] = load_constants(json.loads(SOURCE_PATH.read_text()))


def main() -> int:
    parser = argparse.ArgumentParser(description="Regenerate the page module from link_constants.json.")
    parser.add_argument("--write", action="store_true", help="write the generated TS module")
    args = parser.parse_args()
    target = _REPO_ROOT / GENERATED_TS_REL
    rendered = render_typescript(LINK)
    if args.write:
        target.write_text(rendered)
        print(f"[OK] wrote {GENERATED_TS_REL}")
        return 0
    if target.read_text() != rendered:
        print(f"[ERROR] {GENERATED_TS_REL} is stale: run with --write")
        return 1
    print(f"[OK] {GENERATED_TS_REL} matches the JSON")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
