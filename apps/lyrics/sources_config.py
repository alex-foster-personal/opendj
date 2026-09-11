"""Operator-configurable lyric source ordering. ONE home for the ranking.

The version screen breaks ties (and picks blind-screen winners) by source
provenance. The default order encodes the measured findings to date:
licensed Musixmatch text beats duration-anchored LRCLIB (the fetch fork
measured 4.5% disagreement on exact-get), and search/relaxed methods rank
below their direct counterparts.

the maintainer can reorder this from the admin panel; the persisted order lives in
``data/state/lyrics-config.json`` and is read by BOTH the daemon endpoint
and the pipeline scripts, so what the admin UI shows is exactly what the
next fetch/screen run uses (agent-native parity).

Fail-fast contract: a config file that exists but is malformed, names an
unknown source, or drops a known one RAISES - a silently-ignored config is
how an operator's ordering decision gets lost.
"""

from __future__ import annotations

import json
from pathlib import Path

# "provider method" strings, exactly as stored in lyric_verdict.source.
DEFAULT_SOURCE_ORDER: tuple[str, ...] = (
    "musixmatch matcher-get",
    "musixmatch search-get",
    "musixmatch matcher-get-remix-base",
    "musixmatch matcher-get-no-feat",
    "musixmatch matcher-get-bare",
    "lrclib exact-get",
    "lrclib get",
    "lrclib search",
    "genius scrape",
)
KNOWN_SOURCES: frozenset[str] = frozenset(DEFAULT_SOURCE_ORDER)

SOURCE_TITLES: dict[str, str] = {
    "musixmatch matcher-get": "Musixmatch licensed match (strongest: full text, duration-matched)",
    "musixmatch search-get": "Musixmatch catalogue search then get (catches lyricless duplicates)",
    "musixmatch matcher-get-remix-base": "Musixmatch match on the remix's base title",
    "musixmatch matcher-get-no-feat": "Musixmatch match with the feat. clause stripped",
    "musixmatch matcher-get-bare": "Musixmatch match on bare title only",
    "lrclib exact-get": (
        "LRCLIB exact artist+title+duration (4.5% cross-source disagreement measured)"
    ),
    "lrclib get": "LRCLIB get without duration anchor",
    "lrclib search": "LRCLIB fuzzy search (weakest ranked)",
    "genius scrape": "Genius scrape (leads only: ~24% measured precision)",
}


def config_path(state_dir: Path) -> Path:
    """``state_dir`` is the directory holding state.db (data/state)."""
    return state_dir / "lyrics-config.json"


def load_source_order(state_dir: Path) -> list[str]:
    """The active order: the persisted config when present, else defaults."""
    path = config_path(state_dir)
    if not path.is_file():
        return list(DEFAULT_SOURCE_ORDER)
    raw = json.loads(path.read_text(encoding="utf-8"))
    order = raw.get("source_order")
    _validate_order(order)
    return list(order)


def save_source_order(state_dir: Path, order: list[str]) -> None:
    _validate_order(order)
    path = config_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"source_order": order}, indent=2) + "\n", encoding="utf-8"
    )


def source_rank(order: list[str], source: str) -> int:
    """Rank of a stored ``lyric_verdict.source`` string; unknown/legacy
    strings rank below every configured source rather than erroring, because
    the DB may hold provenance minted before a source was catalogued."""
    try:
        return order.index(source)
    except ValueError:
        return len(order)


def _validate_order(order: object) -> None:
    if not isinstance(order, list) or not all(isinstance(s, str) for s in order):
        raise ValueError("source_order must be a list of strings")
    unknown = [s for s in order if s not in KNOWN_SOURCES]
    if unknown:
        raise ValueError(f"unknown lyric sources: {unknown}; known: {sorted(KNOWN_SOURCES)}")
    missing = KNOWN_SOURCES - set(order)
    if missing:
        raise ValueError(
            f"source_order must rank every known source; missing: {sorted(missing)}"
        )
    if len(order) != len(set(order)):
        raise ValueError("source_order contains duplicates")
