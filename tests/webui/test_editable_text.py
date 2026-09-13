"""Listing row genre/comments prefer webui-sourced EAV over vendor meta."""

from __future__ import annotations

import pytest

from apps.adapters.rekordbox.models import RbRowMeta
from apps.webui.server.backend import Provenance, Track
from apps.webui.server.rb_vendor_pkg import track_rows


def _meta(genre: str | None = "House") -> RbRowMeta:
    return RbRowMeta(
        vendor_id="v1",
        folder_path=None,
        analysis_data_path=None,
        comment=None,
        genre=genre,
        play_count=0,
    )


def _stub_build(monkeypatch: pytest.MonkeyPatch, meta: RbRowMeta) -> None:
    monkeypatch.setattr(
        track_rows, "bulk_rb_meta", lambda stable_ids: {sid: meta for sid in stable_ids}
    )
    monkeypatch.setattr(
        track_rows, "bulk_availability", lambda *a, **k: {sid: True for sid in a[0]}
    )
    monkeypatch.setattr(track_rows, "bulk_quality", lambda *a, **k: {sid: {} for sid in a[0]})
    monkeypatch.setattr(
        track_rows, "bulk_stem_summaries", lambda stable_ids: {sid: {} for sid in stable_ids}
    )


def test_editable_text_prefers_webui_genre_over_vendor_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    track = Track(
        stable_id="sid-1",
        provenance={
            "genre": Provenance(
                value="Deep House",
                source="webui",
                confidence=1.0,
                modified_at="2026-09-13T00:00:00Z",
                status="ok",
            )
        },
    )
    _stub_build(monkeypatch, _meta())
    rows = track_rows.build_track_rows([track])
    assert rows[0]["genre"] == "Deep House"


def test_editable_text_keeps_vendor_genre_when_provenance_is_rekordbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    track = Track(
        stable_id="sid-1",
        provenance={
            "genre": Provenance(
                value="Deep House",
                source="rekordbox",
                confidence=1.0,
                modified_at="2026-09-13T00:00:00Z",
                status="ok",
            )
        },
    )
    _stub_build(monkeypatch, _meta())
    rows = track_rows.build_track_rows([track])
    assert rows[0]["genre"] == "House"
