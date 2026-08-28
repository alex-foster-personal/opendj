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

from apps.adapters.rekordbox import paths as rb_paths
from apps.shared import platform_paths as pp
from apps.webui.server import rb_vendor
from apps.webui.server.rb_vendor_pkg import anlz as rb_anlz
from apps.webui.server.rb_vendor_pkg import anlz_cache as rb_anlz_cache
from apps.webui.server.rb_vendor_pkg import db as rb_db


def test_anlz_cache_hit_overlays_live_cues(monkeypatch: Any, tmp_path: Any) -> None:
    cached_payload = {
        "stable_id": "track-1",
        "waveform": {"kind": "tri"},
        "cues": [{"comment": "obsolete"}],
        "vocals": {"status": "not_analyzed"},
    }
    live_cues = [{"comment": "current"}]
    content = rb_vendor.RbContent(
        stable_id="track-1",
        vendor_id="vendor-1",
        folder_path=None,
        image_path=None,
        analysis_data_path="ANLZ0000.DAT",
        length_s=None,
        comment=None,
        genre=None,
    )

    # Patched on the modules that own these names, not on the rb_vendor
    # facade. Before T3b wave 4 the facade owned them and build_anlz_payload
    # imported them back from it at call time; now each one has a single home
    # and patching the facade would rebind a re-export nothing reads.
    monkeypatch.setattr(rb_paths, "anlz_dir", lambda _content: tmp_path)
    monkeypatch.setattr(
        rb_paths,
        "resolve_asset_path",
        lambda analysis_path: pp.MappedPath(
            original=analysis_path,
            resolved=tmp_path / "ANLZ0000.DAT",
            mapped=True,
            reason="native",
        ),
    )
    monkeypatch.setattr(rb_anlz_cache, "_anlz_mtime", lambda _directory: 1.0)
    monkeypatch.setattr(
        rb_anlz_cache,
        "_load_cached_payload",
        lambda _stable_id, _anlz_mtime, _points: cached_payload,
    )
    monkeypatch.setattr(rb_db, "fetch_cues", lambda _vendor_id: live_cues)
    # This tripwire only bites now that it names the module build_anlz_payload
    # actually resolves _first_tags from. On the facade it was patching a
    # re-export the decoder never consulted, so a cache MISS would have gone
    # unnoticed here.
    monkeypatch.setattr(
        rb_anlz,
        "_first_tags",
        lambda _directory: (_ for _ in ()).throw(AssertionError("cache miss")),
    )

    payload = rb_vendor.build_anlz_payload(content, points=256)

    assert payload["waveform"] == {"kind": "tri"}
    assert payload["cues"] == live_cues
    assert payload is not cached_payload
