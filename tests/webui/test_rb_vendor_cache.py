"""Regression coverage for Rekordbox analysis-cache freshness.

Requirements:

✔︎ A cache hit may reuse static ANLZ-derived waveform data.
✔︎ Cached cue rows must never override the current ``djmdCue`` rows.

Acceptance tests:

* [if] a cached payload contains an obsolete cue [then ⛔️] the response exposes it.
* [if] the live cue query returns a changed cue [then ⛔️] the response omits it.
* [if] the ANLZ cache hits [then ⛔️] static waveform data is reparsed.
"""
from __future__ import annotations

from typing import Any

from apps.webui.server import rb_vendor


def test_anlz_cache_hit_overlays_live_cues(monkeypatch: Any, tmp_path: Any) -> None:
    cached_payload = {
        "stable_id": "track-1",
        "waveform": {"kind": "tri"},
        "cues": [{"comment": "obsolete"}],
    }
    live_cues = [{"comment": "current"}]
    content = rb_vendor.RbContent(
        stable_id="track-1",
        vendor_id="vendor-1",
        folder_path=None,
        image_path=None,
        analysis_data_path=None,
        length_s=None,
        comment=None,
        genre=None,
    )

    monkeypatch.setattr(rb_vendor, "anlz_dir", lambda _content: tmp_path)
    monkeypatch.setattr(rb_vendor, "_anlz_mtime", lambda _directory: 1.0)
    monkeypatch.setattr(
        rb_vendor,
        "_load_cached_payload",
        lambda _stable_id, _anlz_mtime, _points: cached_payload,
    )
    monkeypatch.setattr(rb_vendor, "fetch_cues", lambda _vendor_id: live_cues)
    monkeypatch.setattr(
        rb_vendor,
        "_first_tags",
        lambda _directory: (_ for _ in ()).throw(AssertionError("cache miss")),
    )

    payload = rb_vendor.build_anlz_payload(content, points=256)

    assert payload["waveform"] == {"kind": "tri"}
    assert payload["cues"] == live_cues
    assert payload is not cached_payload
