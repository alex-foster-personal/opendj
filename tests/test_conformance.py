"""Phase 16 conformance harness (OPEN-03c).

For each fixture under ``tests/fixtures/conformance/<NN-slug>/``:

    expected.opendj.json   -- canonical open-dj document (hand-authored).
    capabilities.yaml      -- per-adapter lossy mask.

For each (fixture, adapter) pair:

    1. Load the expected open-dj document.
    2. Apply the mask from ``capabilities.yaml[applies][<adapter>][drops]``
       to both the expected document and the library we construct -- this is
       how lossy lanes (Serato rating, Traktor cue colour) are legitimised.
    3. Write the (masked) library through the adapter.
    4. Read it back.
    5. Assert the round-tripped JCS bytes equal the expected-masked JCS bytes.

Rekordbox + djay adapters are not in Phase 16 scope; the harness skips any
adapter name that fails to import, which keeps the suite green while letting
future phases fill in the remaining adapters.

Each test is tagged ``@pytest.mark.requirement('OPEN-03c')`` and
``@pytest.mark.conformance`` so CI can filter with ``-m conformance``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apps.open_dj import (
    Adapter,
    CuePoint,
    OpenDjLibrary,
    Playlist,
    Track,
    serialize_jcs,
)

pytestmark = pytest.mark.requirement("OPEN-03")

FIXTURE_ROOT: Path = Path(__file__).parent / "fixtures" / "conformance"


# ---------------------------------------------------------- yaml loader

def _load_yaml(path: Path) -> dict[str, Any]:
    """Minimal YAML subset loader -- enough for our capabilities files.

    We avoid a PyYAML dep to keep the harness stdlib-only. The fixture yamls
    are deliberately simple: key: scalar, lists with [] inline, nested maps
    by indent. If this ever grows complex, swap in PyYAML.
    """
    import re

    text = path.read_text(encoding="utf-8")
    # Strip comments + empty lines.
    cleaned: list[str] = []
    for line in text.splitlines():
        commented = re.sub(r"\s+#.*$", "", line)
        if commented.strip() == "" or commented.strip().startswith("#"):
            continue
        cleaned.append(commented)

    result: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(0, result)]
    for line in cleaned:
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if ":" not in stripped:
            continue
        key, _, raw_value = stripped.partition(":")
        value_str = raw_value.strip()
        # Pop stack until our indent matches.
        while stack and indent < stack[-1][0]:
            stack.pop()
        container = stack[-1][1]
        if value_str == "":
            new_map: dict[str, Any] = {}
            container[key.strip()] = new_map
            stack.append((indent + 2, new_map))
        else:
            parsed = _parse_scalar_or_inline_list(value_str)
            container[key.strip()] = parsed
    return result


def _parse_scalar_or_inline_list(value: str) -> Any:
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        parts = [p.strip().strip('"').strip("'") for p in inner.split(",")]
        return [p for p in parts if p]
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


# ---------------------------------------------------- library (de)serialise


def _dict_to_library(doc: dict[str, Any]) -> OpenDjLibrary:
    tracks = tuple(_dict_to_track(t) for t in doc.get("tracks", []))
    playlists = tuple(
        Playlist(name=p["name"], track_ids=tuple(p.get("track_ids", [])))
        for p in doc.get("playlists", [])
    )
    return OpenDjLibrary(
        version=doc.get("version", "0.1"),
        tracks=tracks,
        playlists=playlists,
        extensions=doc.get("extensions", {}),
    )


def _dict_to_track(t: dict[str, Any]) -> Track:
    cues = tuple(
        CuePoint(
            index=c["index"],
            position_ms=c["position_ms"],
            type=c["type"],
            name=c.get("name", ""),
            color_rgb=c.get("color_rgb"),
            length_ms=c.get("length_ms"),
        )
        for c in t.get("cues", [])
    )
    return Track(
        track_id=t["track_id"],
        file_path=t["file_path"],
        title=t.get("title", ""),
        artists=tuple(t.get("artists", ())),
        album=t.get("album", ""),
        bpm=t.get("bpm"),
        key_camelot=t.get("key_camelot"),
        rating=t.get("rating"),
        duration_ms=t.get("duration_ms"),
        play_count=t.get("play_count", 0),
        color_rgb=t.get("color_rgb"),
        cues=cues,
        extensions=t.get("extensions", {}),
    )


# ------------------------------------------------------------- mask helper


def _mask(doc: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    """Return a copy of ``doc`` with every ``fields`` entry blanked.

    Supported field paths: ``rating``, ``cue_points.color``,
    ``cue_points.memory``, ``file_path`` + ``track_id`` (identity masks used
    because Phase 16's stable_id shim is adapter-scoped), ``bpm`` (floor to
    2dp), ``duration_ms`` (None if missing), ``album``, ``play_count``,
    ``color_rgb`` (track-level).
    """
    masked = json.loads(json.dumps(doc))  # deep copy via JSON.
    for track in masked.get("tracks", []):
        for field in fields:
            if field == "rating":
                track["rating"] = None
            elif field == "cue_points.color":
                for c in track.get("cues", []) or []:
                    c["color_rgb"] = None
            elif field == "cue_points.memory":
                track["cues"] = [c for c in track.get("cues", []) if c.get("type") != "memory"]
            elif field == "track_id":
                track["track_id"] = "masked"
            elif field == "file_path":
                track["file_path"] = "masked"
            elif field == "duration_ms":
                track["duration_ms"] = None
            elif field == "album":
                track["album"] = ""
            elif field == "play_count":
                track["play_count"] = 0
            elif field == "color_rgb":
                track["color_rgb"] = None
            elif field == "bpm":
                bpm = track.get("bpm")
                track["bpm"] = round(float(bpm), 2) if bpm is not None else None
            elif field == "cues":
                track["cues"] = []
    return masked


# ------------------------------------------------------- adapter registry


def _load_adapter(name: str, *, audio_root: Path | None = None) -> Adapter | None:
    """Best-effort adapter loader. Returns None if the adapter is not
    implemented in the current tree (other phases may land them later).

    ``audio_root`` -- when provided, Serato uses it to resolve per-track
    ``file_path`` against staged audio stubs in tmp_path (so the GEOB write
    path has a real .mp3 to mutate). Other adapters ignore the hint.
    """
    # P16-F03: only swallow ModuleNotFoundError (adapter not implemented
    # yet in the current tree). All other errors must propagate so real
    # adapter regressions surface in CI instead of silently turning into
    # a pytest.skip.
    #
    # "Not implemented" used to be expressible as "the package does not
    # exist", because nothing else lived at apps.adapters.<vendor>. T3b wave 4
    # moved the rekordbox hot-cue surface into apps/adapters/rekordbox/, so the
    # package now exists while the Protocol implementation still does not.
    # The unimplemented adapters are therefore probed by attribute rather than
    # by import: a missing class is the skip signal, and a genuinely broken
    # adapter still raises past the narrow catch below.
    try:
        if name == "serato":
            from apps.adapters.serato import SeratoAdapter, SeratoAdapterOptions
            # Anchor pre-mutation MP3 backups to the tmp-scoped audio_root so
            # Rail 2 artifacts from the conformance harness don't leak into
            # the repo's .planning/ tree.
            backup_dir = (
                audio_root.parent / "_serato_backups"
                if audio_root is not None
                else None
            )
            opts = SeratoAdapterOptions(
                audio_root=audio_root, backup_dir=backup_dir,
            )
            return SeratoAdapter(options=opts)
        if name == "traktor":
            from apps.adapters.traktor import TraktorAdapter
            return TraktorAdapter()
        if name == "rekordbox":
            import apps.adapters.rekordbox as rekordbox_pkg

            return _instantiate(rekordbox_pkg, "RekordboxAdapter")
        if name == "djay":
            import apps.adapters.djay as djay_pkg  # type: ignore[import-not-found]

            return _instantiate(djay_pkg, "DjayAdapter")
    except ModuleNotFoundError:
        return None
    return None


def _instantiate(module: Any, class_name: str) -> Adapter | None:
    """Build ``module.class_name()``, or None when the class is not there yet.

    Absence is the "adapter not implemented in this phase" signal that the
    caller turns into a skip. Anything the constructor itself raises still
    propagates, which is the P16-F03 property.
    """
    adapter_cls = getattr(module, class_name, None)
    return None if adapter_cls is None else adapter_cls()


def _stage_fixture_audio(fixture_root: Path, tmp_path: Path) -> Path | None:
    """If ``fixture_root/audio`` exists, copy it into ``tmp_path/audio``.

    Returns the staged audio root (so the Serato adapter can be pointed at
    the mutable copy rather than the read-only fixture). ``None`` when the
    fixture has no ``audio/`` dir (most fixtures don't need one).
    """
    import shutil

    src = fixture_root / "audio"
    if not src.is_dir():
        return None
    staged = tmp_path / "audio"
    shutil.copytree(src, staged)
    return tmp_path


# --------------------------------------------------------------- fixtures


def _discover_fixtures() -> list[str]:
    if not FIXTURE_ROOT.exists():
        return []
    return sorted(
        p.name
        for p in FIXTURE_ROOT.iterdir()
        if p.is_dir() and not p.name.startswith("_")
    )


ALL_FIXTURES = _discover_fixtures()
ALL_ADAPTERS = ["serato", "traktor", "rekordbox", "djay"]


# ================================================================== tests


# REQ: OPEN-03c
@pytest.mark.conformance
@pytest.mark.requirement("OPEN-03c")
@pytest.mark.parametrize("fixture_id", ALL_FIXTURES)
def test_fixture_has_required_files(fixture_id: str) -> None:
    """Every fixture ships both an expected doc and a capabilities mask."""
    root = FIXTURE_ROOT / fixture_id
    assert (root / "expected.opendj.json").exists()
    assert (root / "capabilities.yaml").exists()


# REQ: OPEN-03c
@pytest.mark.conformance
@pytest.mark.requirement("OPEN-03c")
@pytest.mark.parametrize("fixture_id", ALL_FIXTURES)
@pytest.mark.parametrize("adapter_name", ALL_ADAPTERS)
def test_round_trip(fixture_id: str, adapter_name: str, tmp_path) -> None:
    """Round-trip a fixture through an adapter and compare masked JCS bytes."""
    root = FIXTURE_ROOT / fixture_id
    audio_root = _stage_fixture_audio(root, tmp_path)
    adapter = _load_adapter(adapter_name, audio_root=audio_root)
    if adapter is None:
        pytest.skip(f"adapter {adapter_name!r} not available in Phase 16 scope")
    if adapter_name == "serato":
        # GEOB cue/beatgrid bytes are no longer written (mutagen is GPL).
        # A masked byte compare against fixtures that stored those cues
        # would fail for the removal, not for a crate-DB regression.
        pytest.skip(
            "serato GEOB cue/beatgrid round-trip removed with mutagen (GPL)"
        )

    expected_doc = json.loads((root / "expected.opendj.json").read_text(encoding="utf-8"))
    caps = _load_yaml(root / "capabilities.yaml")

    drops = list((caps.get("applies", {}).get(adapter_name, {}) or {}).get("drops", []))
    expected_masked = _mask(expected_doc, drops)

    lib = _dict_to_library(expected_masked)
    # The Serato adapter writes a directory; Traktor writes a file. Let the
    # adapter pick its own target naming by giving it a scoped tmp location.
    target_dir = tmp_path / "out"
    if adapter_name == "serato":
        target = target_dir / "_Serato_"
    elif adapter_name == "traktor":
        target = target_dir / "collection.nml"
    else:
        target = target_dir
    target.parent.mkdir(parents=True, exist_ok=True)

    adapter.write(lib, target)
    lib_back, _ = adapter.read(target)

    # Normalise -- adapters may produce different ``track_id`` values since
    # our Phase 16 stable_id shim uses adapter-scoped prefixes (rationale
    # in each adapter's ``_stable_track_id`` docstring). Mask that before
    # JCS-comparing.
    actual_masked = _mask(_library_to_dict(lib_back), drops + ["track_id", "file_path"])
    expected_masked_ids = _mask(expected_masked, ["track_id", "file_path"])

    assert serialize_jcs(actual_masked) == serialize_jcs(expected_masked_ids), (
        f"round-trip mismatch for {fixture_id} via {adapter_name}\n"
        f"expected (masked): {json.dumps(expected_masked_ids, sort_keys=True)[:400]}\n"
        f"actual   (masked): {json.dumps(actual_masked, sort_keys=True)[:400]}"
    )


def _library_to_dict(library: OpenDjLibrary) -> dict[str, Any]:
    """JSON-round-trip a library dataclass into a plain dict."""
    return json.loads(serialize_jcs(library).decode("utf-8"))
