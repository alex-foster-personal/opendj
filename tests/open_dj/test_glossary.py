"""META-06 cross-vendor terminology glossary.

Regression one-liners:
  - if a vendor term is looked up then it resolves to our canonical and to
    every shipped adapter
  - if the glossary covers only rekordbox then broken
  - if the JSON map is not valid JSON or an entry has no canonical then broken
  - if a shipped adapter or a Track/capability field has no glossary entry
    then broken
  - if markdown and JSON canonical sets drift then broken
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from apps.adapters.serato.capabilities import capabilities as serato_capabilities
from apps.adapters.traktor.capabilities import capabilities as traktor_capabilities
from apps.open_dj.registry import ADAPTERS
from apps.open_dj.schema import Track

pytestmark = pytest.mark.requirement("META-06")

REPO = Path(__file__).resolve().parents[2]
MAP_PATH = REPO / "open-dj" / "synonym-map.json"
MARKDOWN_PATH = REPO / "open-dj" / "terminology.md"
SCHEMA_PATH = REPO / "open-dj" / "schema" / "v0.2" / "open-dj.schema.json"

# Track.extensions is a passthrough bag for x_* adapter data, not a vendor
# library concept, so it is the only Track field allowed to sit outside the
# glossary. Wire aliases map onto the canonical names we actually ship.
TRACK_FIELDS_EXCLUDED = frozenset({"extensions"})
WIRE_ALIAS_TO_CANONICAL = {
    "cues": "cue_points",
    "key": "key_camelot",
    "beatgrid": "beats",
}


def _load_map() -> dict:
    with MAP_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def _parse_markdown_table(text: str) -> tuple[list[str], list[list[str]]]:
    """Return (header_cells, data_rows) from the first pipe table in text."""
    rows: list[list[str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and all(set(c) <= {"-", ":"} and c for c in cells):
            continue
        rows.append(cells)
    if not rows:
        raise AssertionError(f"{MARKDOWN_PATH} has no pipe table")
    return rows[0], rows[1:]


def test_synonym_map_is_valid_json() -> None:
    with MAP_PATH.open(encoding="utf-8") as fh:
        payload = json.load(fh)
    assert "schema_version" in payload
    assert "entries" in payload
    assert isinstance(payload["entries"], list)
    assert payload["entries"], "synonym map entries must be non-empty"


def test_every_entry_names_a_canonical_term() -> None:
    payload = _load_map()
    seen: dict[str, int] = {}
    duplicates: list[str] = []
    for index, entry in enumerate(payload["entries"]):
        assert isinstance(entry, dict), f"entries[{index}] is not a dict"
        canonical = entry.get("canonical")
        assert isinstance(canonical, str) and canonical.strip(), (
            f"entries[{index}] has no non-empty canonical term"
        )
        if canonical in seen:
            duplicates.append(canonical)
        seen[canonical] = index
    assert not duplicates, f"duplicate canonical names: {sorted(set(duplicates))}"


@pytest.mark.parametrize(
    ("term", "canonical"),
    [
        ("FolderPath", "file_path"),
        ("BPM", "bpm"),
        ("uuid", "vendor_ids"),
        ("tbpm", "bpm"),
        ("pfil", "file_path"),
        ("tkey", "key_camelot"),
        ("TEMPO @BPM", "bpm"),
        ("MUSICAL_KEY @VALUE", "key_camelot"),
        ("INFO @RANKING", "rating"),
    ],
)
def test_lookup_resolves_vendor_term_to_all_shipped_adapters(
    term: str, canonical: str
) -> None:
    from apps.open_dj.glossary import lookup

    result = lookup(term)
    assert result["canonical"] == canonical, (
        f"{term!r} resolved to {result['canonical']!r}, expected {canonical!r}"
    )
    assert set(result["vendors"]) == set(ADAPTERS), (
        f"{term!r} vendors {sorted(result['vendors'])} != shipped {sorted(ADAPTERS)}"
    )


def test_lookup_unknown_term_raises() -> None:
    from apps.open_dj.glossary import lookup

    with pytest.raises(KeyError):
        lookup("this-term-is-not-in-the-map")


def test_every_shipped_adapter_has_glossary_coverage() -> None:
    shipped = set(ADAPTERS)
    docs = {p.stem for p in (REPO / "open-dj" / "adapters").glob("*.md")}
    assert docs == shipped, (
        f"adapter docs {sorted(docs)} != shipped adapters {sorted(shipped)}"
    )
    payload = _load_map()
    for entry in payload["entries"]:
        canonical = entry["canonical"]
        assert set(entry["vendors"]) == shipped, (
            f"{canonical}: vendors {sorted(entry['vendors'])} != shipped {sorted(shipped)}"
        )
    if "adapters" in payload:
        assert payload["adapters"] == sorted(shipped), (
            f"top-level adapters {payload['adapters']} != {sorted(shipped)}"
        )


def test_every_canonical_field_has_a_glossary_entry() -> None:
    payload = _load_map()
    coverage: set[str] = set()
    for entry in payload["entries"]:
        coverage.add(entry["canonical"])
        coverage.update(entry.get("code_aliases") or [])

    required: set[str] = set()
    required.update(
        f.name for f in dataclasses.fields(Track) if f.name not in TRACK_FIELDS_EXCLUDED
    )
    required.update(field.field for field in serato_capabilities().fields)
    required.update(field.field for field in traktor_capabilities().fields)
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    required.update(schema["$defs"]["Track"]["properties"])

    missing: list[str] = []
    for name in sorted(required):
        if name in coverage:
            continue
        mapped = WIRE_ALIAS_TO_CANONICAL.get(name)
        if mapped is not None and mapped in coverage:
            continue
        missing.append(name)
    assert not missing, (
        "glossary is missing canonical coverage for: " + ", ".join(missing)
    )


def test_markdown_and_json_share_the_same_canonical_set() -> None:
    payload = _load_map()
    json_canonicals = {entry["canonical"] for entry in payload["entries"]}
    text = MARKDOWN_PATH.read_text(encoding="utf-8")
    header, rows = _parse_markdown_table(text)
    for required in ("canonical", "ui", "rekordbox", "djay", "serato", "traktor"):
        assert required in header, f"markdown table missing column {required!r}"
    col = {name: index for index, name in enumerate(header)}
    md_canonicals: set[str] = set()
    blank: list[str] = []
    for row in rows:
        canonical = row[col["canonical"]]
        md_canonicals.add(canonical)
        for vendor in ("rekordbox", "djay", "serato", "traktor"):
            cell = row[col[vendor]]
            if not cell:
                blank.append(f"{canonical}.{vendor}")
    assert md_canonicals == json_canonicals, (
        f"markdown-only={sorted(md_canonicals - json_canonicals)} "
        f"json-only={sorted(json_canonicals - md_canonicals)}"
    )
    assert not blank, f"blank vendor cells: {blank}"


def test_markdown_is_not_rekordbox_only() -> None:
    text = MARKDOWN_PATH.read_text(encoding="utf-8")
    lowered = text.lower()
    for name in ("rekordbox", "djay", "serato", "traktor"):
        assert name in lowered, f"{MARKDOWN_PATH} never mentions {name}"
    header, _rows = _parse_markdown_table(text)
    for vendor in ("rekordbox", "djay", "serato", "traktor"):
        assert vendor in header, f"table header missing vendor column {vendor!r}"
