"""Strict top-level schema check for a skin manifest (``(private path)/<id>/skin.json``).

Follows the draft manifest in ``(private path)`` section 2, plus
``contrast_pairs`` which ``scripts/skins/contrast_check.py`` reads. A bad manifest
raises ``SkinManifestError`` naming the key path and the allowed values.

Usage:
  uv run --locked python -m scripts.skins.manifest_schema (private path)/*/skin.json
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ALLOWED_KEYS = frozenset(
    {
        "id",
        "family",
        "display_name",
        "inspired_by_note",
        "tokens",
        "layout",
        "controls",
        "practice",
        "evidence",
        "legal",
        "contrast_pairs",
    }
)
REQUIRED_KEYS = ("id", "family", "display_name", "tokens")
FAMILIES = ("software", "hardware", "house")
_ID_RE = re.compile(r"^[a-z0-9-]+$")


class SkinManifestError(ValueError):
    """A skin manifest does not match the strict schema."""


def validate_skin_manifest(data: Any, folder: str | None = None) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise SkinManifestError("$: manifest must be an object")
    unknown = sorted(set(data) - ALLOWED_KEYS)
    if unknown:
        raise SkinManifestError(
            f"$.{unknown[0]}: unknown key {unknown[0]!r}; allowed keys: {', '.join(sorted(ALLOWED_KEYS))}"
        )
    for key in REQUIRED_KEYS:
        if key not in data:
            raise SkinManifestError(f"$.{key}: required key missing")
    skin_id = data["id"]
    if not isinstance(skin_id, str) or not _ID_RE.match(skin_id):
        raise SkinManifestError(f"$.id: {skin_id!r} must match [a-z0-9-]+")
    if data["family"] not in FAMILIES:
        raise SkinManifestError(f"$.family: {data['family']!r} not allowed; allowed values: {', '.join(FAMILIES)}")
    if not isinstance(data["tokens"], dict):
        raise SkinManifestError("$.tokens: must be an object")
    if folder is not None and skin_id != folder:
        raise SkinManifestError(f"$.id: id {skin_id!r} differs from folder name {folder!r}")
    return data


def load_skin_manifest(path: str | Path, folder: str | None = None) -> dict[str, Any]:
    """Load and validate ``path``. ``folder`` defaults to the parent directory name."""
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    return validate_skin_manifest(data, p.parent.name if folder is None else folder)


def main(argv: list[str] | None = None) -> int:
    rc = 0
    for arg in argv if argv is not None else sys.argv[1:]:
        try:
            load_skin_manifest(arg)
        except (SkinManifestError, json.JSONDecodeError) as exc:
            print(f"{arg}: {exc}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
