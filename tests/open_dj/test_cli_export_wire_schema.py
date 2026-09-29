"""Regression: ``open-dj-tool export`` must emit the v0.2 wire format.

Codex Phase-15 review flagged that the class-family CLI path (serato /
traktor) was passing ``dataclasses.asdict`` output straight to disk
instead of the ProvenanceValue-wrapped v0.2 shape defined in
``open-dj/schema/v0.2/open-dj.schema.json``. This test builds a minimal
typed :class:`OpenDjLibrary`, exercises the wire serializer directly,
and validates the result against the shipped JSON Schema. It also walks
through the CLI ``export`` subcommand end-to-end to pin the wiring.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from apps.open_dj import (
    BeatGridPoint,
    CuePoint,
    OpenDjLibrary,
    Playlist,
    Track,
)
from apps.open_dj.cli import main
from apps.open_dj.validate import validate_document
from apps.open_dj.wire import library_to_wire_document

# A valid SHA-1-style 40-hex track id and SHA-256 content hash, matching
# the schema's regexes (``^[0-9a-f]{40}$`` and ``^sha256:[0-9a-f]{64}$``).
_TRACK_ID = "a" * 40
_CONTENT_HASH = "sha256:" + ("b" * 64)
# library_to_wire_document() requires an explicit modified_at (no
# wall-clock default -- see apps.open_dj.provenance module docstring).
_MODIFIED_AT = datetime(2026, 1, 5, 12, 30, 0, tzinfo=UTC)


def _minimal_library() -> OpenDjLibrary:
    return OpenDjLibrary(
        version="0.2",
        tracks=(
            Track(
                track_id=_TRACK_ID,
                file_path="/Music/min/one.flac",
                title="One",
                artists=("Ada",),
                duration_ms=123_000,
                bpm=124.0,
                key_camelot="8A",
                rating=5,
                cues=(
                    CuePoint(index=0, position_ms=0, type="hot", name="Intro"),
                    CuePoint(
                        index=1,
                        position_ms=32_000,
                        type="loop",
                        name="Loop8",
                        length_ms=4_000,
                    ),
                ),
                beats=(
                    BeatGridPoint(position_ms=0, bpm=124.0),
                    BeatGridPoint(position_ms=483, bpm=124.0),
                ),
                isrc="USRC17607839",
                extensions={"content_hash": _CONTENT_HASH},
            ),
        ),
        playlists=(
            Playlist(
                name="Warmups",
                track_ids=(_TRACK_ID,),
            ),
        ),
    )


@pytest.mark.requirement("OPEN-01a")
class TestExportV02WireSchema:
    """[if] the v02 export schema is used [then] its wire contract remains stable, [else stop]."""
    """Export must produce schema-valid v0.2 documents (codex P15 finding)."""

    def test_library_to_wire_document_validates(self) -> None:
        doc = library_to_wire_document(
            _minimal_library(), source="serato", modified_at=_MODIFIED_AT
        )

        # Top-level shape: spec-mandated keys present, no stale ones.
        assert doc["schema_version"] == "0.2"
        assert doc["kind"] == "library"
        assert "tracks" in doc
        assert "playlists" in doc
        # The in-memory typed ``version`` must not leak to the wire doc.
        assert "version" not in doc

        errors = validate_document(doc)
        assert errors == [], (
            "wire document failed v0.2 schema validation:\n  "
            + "\n  ".join(errors)
        )

    def test_provenance_envelopes_on_authored_fields(self) -> None:
        doc = library_to_wire_document(
            _minimal_library(), source="serato", modified_at=_MODIFIED_AT
        )
        track = doc["tracks"][0]

        # bpm / key / rating must be ProvenanceValue objects, not scalars.
        for field in ("bpm", "key", "rating"):
            assert isinstance(track[field], dict), (
                f"{field} should be wrapped in a ProvenanceValue dict"
            )
            assert track[field]["source"] == "serato"
            assert "modified_at" in track[field]
            assert "value" in track[field]

        assert track["bpm"]["value"] == pytest.approx(124.0)
        assert track["key"]["value"] == "8A"
        assert track["rating"]["value"] == 5

    def test_cue_loop_alias_splits_into_loop_in_loop_out(self) -> None:
        doc = library_to_wire_document(
            _minimal_library(), source="serato", modified_at=_MODIFIED_AT
        )
        cues = doc["tracks"][0]["cue_points"]
        types = [c["type"] for c in cues]
        assert "loop" not in types, "convenience alias must not leak to wire"
        assert types.count("loop_in") == 1
        assert types.count("loop_out") == 1

        loop_in = next(c for c in cues if c["type"] == "loop_in")
        loop_out = next(c for c in cues if c["type"] == "loop_out")
        assert loop_out["position_ms"] == loop_in["position_ms"] + 4_000

    def test_beats_collapse_to_beatgrid_object(self) -> None:
        doc = library_to_wire_document(
            _minimal_library(), source="serato", modified_at=_MODIFIED_AT
        )
        track = doc["tracks"][0]
        assert "beats" not in track, "in-memory per-anchor shape must not leak"
        grid = track["beatgrid"]
        assert grid["origin_ms"] == 0.0
        assert grid["bpm"] == pytest.approx(124.0)
        assert grid["algorithm"] == "constant"
        assert grid["beats"] == [0.0, 483.0]

    def test_cli_export_validates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``open-dj-tool export`` end-to-end must write a schema-valid file."""
        library = _minimal_library()

        class _FakeClassAdapter:
            def read(self, source: Path) -> tuple[Any, Any]:
                # AdapterReport is mutable; a stub object is fine because
                # the CLI does not inspect it on the export path.
                return library, object()

            def write(self, library: Any, target: Path) -> Any:  # pragma: no cover
                return object()

            def capabilities(self) -> Any:  # pragma: no cover
                return object()

        # The real ``serato`` registry entry is family="class"; swapping
        # the loader is all we need to take the class-family branch with
        # our stand-in adapter.
        monkeypatch.setattr(
            "apps.open_dj.cli.load_adapter", lambda _name: _FakeClassAdapter()
        )

        source = tmp_path / "library.serato"
        source.write_bytes(b"")
        out = tmp_path / "library.open-dj.json"

        rc = main(
            [
                "export",
                "--adapter",
                "serato",
                "--source",
                str(source),
                "--out",
                str(out),
            ]
        )
        assert rc == 0, "export should succeed"
        assert out.exists()

        doc = json.loads(out.read_bytes())
        errors = validate_document(doc)
        assert errors == [], (
            "CLI-exported document failed v0.2 schema validation:\n  "
            + "\n  ".join(errors)
        )
