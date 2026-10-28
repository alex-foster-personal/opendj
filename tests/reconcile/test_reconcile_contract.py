"""apps.reconcile is reached through its contract (ADR-NEW-module-contracts, M1)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from apps.reconcile import locate
from apps.reconcile.contract import (
    CONTRACT_VERSION,
    Candidate,
    ReconcileService,
    RelocateQuery,
)
from apps.reconcile.http import build_service
from apps.shared.hashing import sha256_file

# Real committed media, verified by checksum before use (AGENTS.md "No mocks
# and locked real fixtures"); the scorer reads their tags through tinytag.
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup"
_MOVED = ("src-128.mp3", "sha256:922d6cfa0886ef5a6ae195af01d992782d2680bc40244940254934c9aee7c4d0")
_OTHER = ("other-silent-intro.mp3", "sha256:db8d8671be0f6375714e8fefde362ec9d8e43bcc3c725c82a97ba305e9baf45a")


def _verified(name: str, digest: str) -> bytes:
    src = _FIXTURES / name
    assert sha256_file(src) == digest, f"fixture {src} changed; it is locked"
    return src.read_bytes()


def test_built_service_satisfies_protocol_and_scores_through_locate(tmp_path: Path) -> None:
    root = tmp_path / "music"
    root.mkdir()
    moved = _verified(*_MOVED)
    (root / "Moved Track.mp3").write_bytes(moved)
    (root / "Unrelated.mp3").write_bytes(_verified(*_OTHER))

    service: ReconcileService = build_service(music_roots=[root], dedup_db=tmp_path / "absent-dedup.db")
    assert isinstance(service, ReconcileService)
    assert CONTRACT_VERSION == "reconcile/2"

    found = service.find_relocate_candidates(
        RelocateQuery(
            original_path="/gone/Moved Track.mp3",
            basename="Moved Track.mp3",
            duration_s="3.06",
            file_size=str(len(moved)),
        ),
        limit=5,
    )
    assert [c.path.name for c in found] == ["Moved Track.mp3"]
    # duration_match needs the real decoder's duration, so it proves the
    # file went through the production tag reader, not only the name match.
    assert {"basename_exact", "size_match", "duration_match"} <= set(found[0].signals)
    # Same class the locate pipeline builds: moved, not converted.
    assert locate.Candidate is Candidate
    assert isinstance(found[0], Candidate)


def test_relocate_query_rejects_extra_fields_and_is_frozen() -> None:
    with pytest.raises(ValidationError):
        RelocateQuery(original_path="/a.mp3", basename="a.mp3", bogus="x")  # type: ignore[call-arg]
    q = RelocateQuery(original_path="/a.mp3", basename="a.mp3")
    with pytest.raises(ValidationError):
        q.title = "changed"  # type: ignore[misc]
    assert q.as_row() == {
        "original_path": "/a.mp3",
        "basename": "a.mp3",
        "title": "",
        "artist": "",
        "duration_s": "",
        "file_size": "",
        "stable_id": "",
    }
