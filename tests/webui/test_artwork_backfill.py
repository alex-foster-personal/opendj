"""One-time online cover lookup for newly ingested tracks (LIBM-187).

``cover_outcome`` runs the real artwork chain against a real state.db and a
real file with no embedded picture; only the network is a recording double
that answers with MusicBrainz's real response shape. ``CoverBackfill`` is driven
with an injected per-track function so its queueing is tested on its own.

Regression one-liners:
  - [if] an ingested title+artist-only track is not looked up online [then] broken, [else stop].
  - [if] a track with an embedded or folder cover is looked up online [then] broken, [else stop].
  - [if] a track with no artist or title sends a request [then] broken, [else stop].
  - [if] a track that ends with no cover leaves no log line naming why [then] broken, [else stop].
  - [if] scheduling blocks the ingest request on the lookup [then] broken, [else stop].
  - [if] a full queue raises instead of logging the dropped id [then] broken, [else stop].
  - [if] one failing lookup stops the worker for the rest [then] broken, [else stop].
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from apps.adapters.rekordbox import config as rb_config
from apps.shared import artwork_sources as art
from apps.shared.state import db as state_db
from apps.webui.server import artwork_backfill
from apps.webui.server.artwork_backfill import CoverBackfill, Outcome

pytestmark = pytest.mark.requirement("LIBM-187")

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup"
SID = "a" * 40
MB_EMPTY = (200, json.dumps({"recordings": []}).encode())
#: Deezer and iTunes answering with no hits (LIBM-188), so a MusicBrainz miss stays a miss.
NO_STORE_HITS = {
    art.DEEZER_URL: (200, json.dumps({"data": []}).encode()),
    art.ITUNES_URL: (200, json.dumps({"results": []}).encode()),
}


class RecordingHttp:
    def __init__(self, routes: dict[str, tuple[int, bytes]]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url: str) -> tuple[int, bytes]:
        self.calls.append(url)
        for prefix, answer in self.routes.items():
            if url.startswith(prefix):
                return answer
        return 404, b""


def _jpeg() -> bytes:
    out = BytesIO()
    Image.new("RGB", (16, 16), (200, 30, 30)).save(out, format="JPEG")
    return out.getvalue()


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A state.db holding one folder-ingested track with no cover anywhere."""
    track_dir = tmp_path / "_ingest" / "batch"
    track_dir.mkdir(parents=True)
    audio = track_dir / "Aria - ASCO.mp3"
    shutil.copy2(FIXTURE_ROOT / "src-320.mp3", audio)
    state_path = tmp_path / "state" / "state.db"

    def insert(title: str | None, artists: list[str]) -> None:
        conn = state_db.open_rw(state_path)
        try:
            conn.execute(
                "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, duration_ms, "
                "file_path, created_at, updated_at) "
                "VALUES (?, 'inferred', ?, ?, 207438, ?, '2026-10-26', '2026-10-26')",
                (SID, title, json.dumps(artists), str(audio)),
            )
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setenv("ODJ_ARTWORK_ONLINE", "1")
    monkeypatch.setattr(art._BACKOFF, "until", 0.0)
    return insert, audio, state_path.parent / "artwork-cache"


def test_title_artist_only_track_is_looked_up_and_cached(library, monkeypatch) -> None:
    insert, _audio, cache_dir = library
    insert("Aria", ["ASCO"])
    cover = _jpeg()
    recording = {"title": "Aria", "length": 207_000, "artist-credit": [{"name": "ASCO"}],
                 "releases": [{"id": "r1", "release-group": {"id": "rg-1"}}]}
    http = RecordingHttp({
        art.MB_URL: (200, json.dumps({"recordings": [recording]}).encode()),
        art.CAA_URL.format(rg="rg-1"): (200, cover),
    })
    monkeypatch.setattr(art, "_HTTP", http)
    assert artwork_backfill.cover_outcome(SID).status == "found"
    assert art.ArtworkCache(cache_dir).get(SID) == (cover, "image/jpeg")
    assert artwork_backfill.cover_outcome(SID).status == "has_cover"
    assert len(http.calls) == 2, "a cached cover is never looked up again"


def test_miss_is_cached_with_a_reason_naming_the_track(library, monkeypatch) -> None:
    insert, _audio, cache_dir = library
    insert("Aria", ["ASCO"])
    http = RecordingHttp({art.MB_URL: MB_EMPTY, **NO_STORE_HITS})
    monkeypatch.setattr(art, "_HTTP", http)
    outcome = artwork_backfill.cover_outcome(SID)
    assert outcome.status == "miss"
    assert outcome.reason == (
        "musicbrainz: no matching recording (MusicBrainz has no recording with this title by "
        "this artist); deezer: no artist+title match in 0 results; itunes: no artist+title match "
        "in 0 results (title='Aria', artist='ASCO')"
    )
    assert art.ArtworkCache(cache_dir).recent_miss(SID, time.time())
    assert artwork_backfill.cover_outcome(SID).status == "miss"
    assert len(http.calls) == 3, "a cached miss is not looked up again within the week (MB, Deezer, iTunes once)"


def test_folder_cover_means_no_request(library, monkeypatch) -> None:
    insert, audio, _cache = library
    insert("Aria", ["ASCO"])
    (audio.parent / "cover.jpg").write_bytes(_jpeg())
    http = RecordingHttp({})
    monkeypatch.setattr(art, "_HTTP", http)
    assert artwork_backfill.cover_outcome(SID).status == "has_cover"
    assert http.calls == []


def test_no_artist_means_no_request(library, monkeypatch) -> None:
    insert, _audio, _cache = library
    insert("Aria", [])
    http = RecordingHttp({})
    monkeypatch.setattr(art, "_HTTP", http)
    outcome = artwork_backfill.cover_outcome(SID)
    assert outcome.status == "skipped" and "no title and artist" in outcome.reason
    assert http.calls == []


def test_unreachable_network_is_deferred_not_a_miss(library, monkeypatch) -> None:
    insert, _audio, cache_dir = library
    insert("Aria", ["ASCO"])
    monkeypatch.setattr(art, "_HTTP", RecordingHttp({art.MB_URL: (art.NETWORK_FAILURE, b"")}))
    assert artwork_backfill.cover_outcome(SID).status == "deferred"
    assert art.ArtworkCache(cache_dir).miss_reason(SID) is None


@pytest.mark.requirement("ENRICH-09")
def test_forget_miss_lets_a_rerun_ask_again(library, monkeypatch) -> None:
    insert, _audio, cache_dir = library
    insert("Aria", ["ASCO"])
    http = RecordingHttp({art.MB_URL: MB_EMPTY, **NO_STORE_HITS})
    monkeypatch.setattr(art, "_HTTP", http)
    assert artwork_backfill.cover_outcome(SID).status == "miss"
    cache = art.ArtworkCache(cache_dir)
    cache.forget_miss(SID)
    assert cache.miss_reason(SID) is None
    assert artwork_backfill.cover_outcome(SID).status == "miss"
    assert len(http.calls) == 6, "the forgotten miss was looked up again (MB, Deezer, iTunes twice)"


# ----- queue ------------------------------------------------------------------


def test_each_track_gets_one_log_line_and_a_failure_does_not_stop_the_rest(caplog) -> None:
    def run_one(stable_id: str) -> Outcome:
        if stable_id == "boom":
            raise RuntimeError("bad row")
        if stable_id == "hit":
            return Outcome("found", "online cover cached")
        return Outcome("miss", "musicbrainz: no matching recording")

    seen: dict[str, str] = {}
    backfill = CoverBackfill(run_one, on_outcome=lambda sid, outcome: seen.__setitem__(sid, outcome.status))
    with caplog.at_level(logging.INFO, logger=artwork_backfill.__name__):
        assert backfill.schedule(["boom", "hit", "nope"]) == 3
        backfill.join()
    # ENRICH-07: every outcome, a raised lookup included, reaches the coordinator.
    assert seen == {"boom": "error", "hit": "found", "nope": "miss"}
    lines = [r.getMessage() for r in caplog.records]
    assert any("boom: lookup raised" in m for m in lines)
    assert "artwork backfill hit: found (online cover cached)" in lines
    assert "artwork backfill nope: no cover, miss: musicbrainz: no matching recording" in lines
    miss = next(r for r in caplog.records if "nope" in r.getMessage())
    assert miss.levelno == logging.WARNING, "misses must reach engine-warn.log"


def test_schedule_never_blocks_and_a_full_queue_drops_with_a_log(caplog) -> None:
    started, release = threading.Event(), threading.Event()

    def run_one(_stable_id: str) -> Outcome:
        started.set()
        release.wait(5)
        return Outcome("found", "x")

    backfill = CoverBackfill(run_one, maxsize=1)
    t0 = time.monotonic()
    assert backfill.schedule(["first"]) == 1
    assert started.wait(5)
    with caplog.at_level(logging.WARNING, logger=artwork_backfill.__name__):
        assert backfill.schedule(["second", "third"]) == 1
    assert time.monotonic() - t0 < 1.0, "the ingest request must not wait for lookups"
    assert any("queue full" in r.getMessage() and "third" in r.getMessage() for r in caplog.records)
    release.set()
    backfill.join()
