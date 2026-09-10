"""Cross-vendor terminology glossary loader (META-06).

``open-dj/synonym-map.json`` is the machine-readable twin of
``open-dj/terminology.md``. This module loads that map with a standard JSON
reader and resolves a vendor, code, or UI term to the full entry covering
every shipped adapter.

CLI::

    python -m apps.open_dj.glossary lookup <term>
    python -m apps.open_dj.glossary dump
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from apps.open_dj.registry import ADAPTERS

MAP_PATH: Path = Path(__file__).resolve().parents[2] / "open-dj" / "synonym-map.json"

_LOOKUP_SCALAR_FIELDS = ("canonical", "code", "ui")
_LOOKUP_LIST_FIELDS = ("code_aliases", "ui_aliases")


def load_synonym_map(path: Path | None = None) -> dict[str, Any]:
    """Load and validate ``open-dj/synonym-map.json``.

    Raises:
        FileNotFoundError: the map file is missing.
        json.JSONDecodeError: the file is not valid JSON.
        ValueError: an entry lacks ``canonical`` or ``vendors``, a vendor
            key is missing or extra, or two entries share a lookup key.
    """
    map_path = MAP_PATH if path is None else path
    payload = json.loads(map_path.read_text(encoding="utf-8"))
    _validate_map(payload)
    return payload


def lookup(term: str) -> dict[str, Any]:
    """Resolve ``term`` to a full glossary entry.

    Match is case-insensitive against canonical, code, code aliases, UI,
    UI aliases, every vendor term, and every vendor synonym. Raises
    ``KeyError`` when nothing matches. The returned ``vendors`` object
    always includes every shipped adapter.
    """
    key = term.strip()
    if not key:
        raise KeyError(term)
    index = _build_lookup_index(load_synonym_map())
    try:
        return index[key.casefold()]
    except KeyError:
        raise KeyError(term) from None


def _validate_map(payload: object) -> None:
    if not isinstance(payload, dict):
        raise TypeError("synonym map must be a JSON object")
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("synonym map entries must be a non-empty list")
    shipped = set(ADAPTERS)
    seen_canonical: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise TypeError(f"entries[{index}] is not an object")
        canonical = entry.get("canonical")
        if not isinstance(canonical, str) or not canonical.strip():
            raise ValueError(f"entries[{index}] has no canonical term")
        if canonical in seen_canonical:
            raise ValueError(f"duplicate canonical {canonical!r}")
        seen_canonical.add(canonical)
        vendors = entry.get("vendors")
        if not isinstance(vendors, dict):
            raise TypeError(f"{canonical}: missing vendors object")
        names = set(vendors)
        if names != shipped:
            missing = shipped - names
            extra = names - shipped
            raise ValueError(
                f"{canonical}: vendors {sorted(names)} != shipped "
                f"{sorted(shipped)}; missing={sorted(missing)} extra={sorted(extra)}"
            )
        for adapter, vendor in vendors.items():
            if not isinstance(vendor, dict):
                raise TypeError(f"{canonical}.{adapter}: vendor entry is not an object")
    _build_lookup_index(payload)


def _entry_lookup_keys(entry: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for field in _LOOKUP_SCALAR_FIELDS:
        value = entry.get(field)
        if isinstance(value, str) and value.strip():
            keys.append(value)
    for field in _LOOKUP_LIST_FIELDS:
        keys.extend(
            item for item in entry.get(field) or [] if isinstance(item, str) and item.strip()
        )
    vendors = entry.get("vendors") or {}
    for vendor in vendors.values():
        if not isinstance(vendor, dict):
            continue
        term = vendor.get("term")
        if isinstance(term, str) and term.strip():
            keys.append(term)
        keys.extend(
            synonym
            for synonym in vendor.get("synonyms") or []
            if isinstance(synonym, str) and synonym.strip()
        )
    return keys


def _build_lookup_index(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    owner: dict[str, str] = {}
    for entry in payload["entries"]:
        canonical = entry["canonical"]
        for raw in _entry_lookup_keys(entry):
            folded = raw.strip().casefold()
            previous = owner.get(folded)
            if previous is not None and previous != canonical:
                raise ValueError(
                    f"duplicate lookup key {raw!r} on {canonical!r} and {previous!r}"
                )
            owner[folded] = canonical
            index[folded] = entry
    return index


def _cmd_lookup(term: str) -> int:
    try:
        entry = lookup(term)
    except KeyError:
        print(f"unknown term: {term}", file=sys.stderr)
        return 2
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        print(f"synonym map error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(entry, indent=2, ensure_ascii=False))
    return 0


def _cmd_dump() -> int:
    try:
        payload = load_synonym_map()
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        print(f"synonym map error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m apps.open_dj.glossary",
        description="Look up a vendor, code, or UI term in the open-dj glossary.",
    )
    sub = parser.add_subparsers(dest="command")

    p_lookup = sub.add_parser("lookup", help="Print the glossary entry for a term.")
    p_lookup.add_argument("term")

    sub.add_parser("dump", help="Print the parsed synonym map.")

    args = parser.parse_args(argv)
    if args.command == "lookup":
        return _cmd_lookup(args.term)
    if args.command == "dump":
        return _cmd_dump()
    parser.print_help(sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
