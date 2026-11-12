"""Fingerprint identify client and proposal store (issue #10429).

Regression one-liners:
  - [if] score >= 0.9 with one recording [then] confident proposal, [else stop].
  - [if] AcoustID answers 503 [then] IdentifyDeferred and backoff, [else stop].
  - [if] ODJ_ACOUSTID_KEY unset [then] IdentifyError, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from apps.tags import identify as mod
from apps.tags.identify import Candidate, IdentifyDeferred, IdentifyError, IdentifyProposal, _Throttle
from apps.tags.title_fix_store import IdentifyItem, ProposalStore, StorePaths

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "identify"

pytestmark = pytest.mark.requirement("SETPREP-12099")


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    mod.reset_for_tests()
    monkeypatch.setenv("ODJ_ACOUSTID_KEY", "test-key")


def test_confident_single_recording() -> None:
    ua_seen: list[str] = []
    bodies = {
        "acoustid": _load("acoustid_single.json"),
        "recording": _load("mb_recording.json"),
        "release": _load("mb_release.json"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        ua_seen.append(request.headers.get("User-Agent", ""))
        if "acoustid" in str(request.url):
            return httpx.Response(200, json=bodies["acoustid"])
        if "/recording/" in str(request.url):
            return httpx.Response(200, json=bodies["recording"])
        if "/release/" in str(request.url):
            return httpx.Response(200, json=bodies["release"])
        return httpx.Response(404)

    out = mod.identify("fp", 240.0, client=_client(handler), key="k")
    assert out is not None
    assert out.status == "confident"
    assert out.best == Candidate("rec-mbid-1", "Test Artist", "Test Track", 1998, "Test Label")
    assert len(ua_seen) == 3
    assert all(u.startswith("OpenDJ/") and "music-dj-tools" in u for u in ua_seen)


def test_needs_review_two_recordings() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "acoustid" in str(request.url):
            return httpx.Response(200, json=_load("acoustid_multi.json"))
        if "rec-a" in str(request.url):
            return httpx.Response(200, json=_load("mb_recording_basic.json"))
        if "rec-b" in str(request.url):
            data = dict(_load("mb_recording_basic.json"))
            data["id"] = "rec-b"
            data["title"] = "Candidate B"
            return httpx.Response(200, json=data)
        return httpx.Response(404)

    out = mod.identify("fp", 100.0, client=_client(handler), key="k")
    assert out is not None
    assert out.status == "needs_review"
    assert len(out.candidates) == 2
    assert not any("/release/" in u for u in calls)


def test_low_score_needs_review() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "acoustid" in str(request.url):
            return httpx.Response(200, json=_load("acoustid_low.json"))
        return httpx.Response(200, json=_load("mb_recording_basic.json"))

    out = mod.identify("fp", 100.0, client=_client(handler), key="k")
    assert out is not None and out.status == "needs_review"


def test_empty_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_load("acoustid_empty.json"))

    assert mod.identify("fp", 1.0, client=_client(handler), key="k") is None


def test_503_sets_backoff() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with pytest.raises(IdentifyDeferred):
        mod.identify("fp", 1.0, client=_client(handler), key="k")
    with pytest.raises(IdentifyDeferred):
        mod.identify("fp", 1.0, client=_client(handler), key="k")


def test_spacing_at_least_one_second() -> None:
    times = [0.0, 0.2, 1.25, 2.3]
    idx = {"i": 0}
    sleeps: list[float] = []

    def clock() -> float:
        return times[idx["i"]]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        idx["i"] += 1

    throttle = _Throttle(clock=clock, sleep=sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        if "acoustid" in str(request.url):
            return httpx.Response(200, json=_load("acoustid_empty.json"))
        return httpx.Response(404)

    mod.identify("fp", 1.0, client=_client(handler), key="k", throttle=throttle, clock=clock)
    mod.identify("fp", 1.0, client=_client(handler), key="k", throttle=throttle, clock=clock)
    assert sleeps and sleeps[0] >= 0.79


def test_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ODJ_ACOUSTID_KEY", raising=False)
    assert mod.api_key() is None
    with pytest.raises(IdentifyError, match="ODJ_ACOUSTID_KEY"):
        mod.identify("fp", 1.0, key=None)


def test_malformed_acoustid_json_raises_identify_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "acoustid" in str(request.url):
            return httpx.Response(200, content=b"not-json")
        return httpx.Response(404)

    with pytest.raises(IdentifyError, match="invalid JSON"):
        mod.identify("fp", 1.0, client=_client(handler), key="k")


def test_add_identify_proposals_needs_review(tmp_path: Path) -> None:
    store = ProposalStore(StorePaths(tmp_path / "state" / "state.db"))
    proposal = IdentifyProposal(
        status="needs_review",
        score=0.5,
        best=Candidate("mbid-a", "Artist A", "Title A", None, None),
        candidates=(
            Candidate("mbid-a", "Artist A", "Title A", None, None),
            Candidate("mbid-b", "Artist B", "Title B", None, None),
        ),
    )
    rows = store.add_identify_proposals([IdentifyItem("t1", "filetitle", "", [], proposal)])
    assert rows[0].fixer_status == "review"
    assert any("candidate:" in note for note in rows[0].notes)


def test_add_identify_proposals_apply_refusal_empty(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from apps.webui.server.title_fix_apply import _apply_refusal

    store = ProposalStore(StorePaths(tmp_path / "state" / "state.db"))
    proposal = IdentifyProposal(
        status="confident",
        score=0.95,
        best=Candidate("mbid", "Artist", "Title", 1999, "Label"),
        candidates=(Candidate("mbid", "Artist", "Title", 1999, "Label"),),
    )
    rows = store.add_identify_proposals([IdentifyItem("t1", "filetitle", "", [], proposal)])
    track = SimpleNamespace(title="filetitle", artist="")
    assert _apply_refusal(rows[0], track, set()) == ""


def test_add_identify_proposals(tmp_path: Path) -> None:
    store = ProposalStore(StorePaths(tmp_path / "state" / "state.db"))
    proposal = IdentifyProposal(
        status="confident",
        score=0.95,
        best=Candidate("mbid", "Artist", "Title", 1999, "Label"),
        candidates=(Candidate("mbid", "Artist", "Title", 1999, "Label"),),
    )
    rows = store.add_identify_proposals(
        [
            IdentifyItem("t1", "filetitle", "", [], proposal),
        ]
    )
    assert rows[0].source == "identify"
    assert rows[0].fixer_status == "fixed"
    assert "musicbrainz recording: mbid" in rows[0].notes[0]
    assert store.running_scan() is None
