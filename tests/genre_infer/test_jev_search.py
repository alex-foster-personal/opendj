"""Cmd K AI search: one JEV call ranks a shortlist; no key or a bad answer is an error, never a ranking.

The reply is real: tests/fixtures/jev/cmdk_ai_search.json was captured from the
live OpenRouter decisions API on Wed 14 Oct 2026 through the production
``build_request`` and ``_http_post``, over invented tracks. Tests replay it in
place of the HTTP call and never call out.

Acceptance lines (CMDK-20):
- [if] a shortlist and words are sent [then] ONE request carries every track and one question each, [else stop].
- [if] JEV answers every track [then] each stable_id gets its probability, [else stop].
- [if] no key [then] SearchKeyMissing and no call, [else stop].
- [if] an answer is missing a track or out of range [then] SearchFailed, never a partial ranking, [else stop].

The HTTP route over this is tested in tests/webui/test_cmdk_ai_routes.py.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from apps.genre_infer import jev, jev_search
from tests.genre_infer.jev_support import captured

pytestmark = pytest.mark.requirement("CMDK-20")


def _fixture() -> dict[str, Any]:
    return captured("cmdk_ai_search")


def _tracks(fx: dict[str, Any]) -> list[jev_search.SearchTrack]:
    return [
        jev_search.SearchTrack(t["stable_id"], {k: v for k, v in t.items() if k != "stable_id"}) for t in fx["tracks"]
    ]


def test_the_request_is_the_one_that_was_captured() -> None:
    fx = _fixture()
    body = jev_search.build_request(fx["words"], _tracks(fx), fx["request"]["model"])
    assert body == fx["request"]
    assert sorted(body["questions"]) == sorted(f"t{i}" for i in range(1, len(fx["tracks"]) + 1))
    assert all(q["type"] == "noul" for q in body["questions"].values())
    # Every track is in the one STATE, by number.
    for i, t in enumerate(fx["tracks"], start=1):
        assert f"Track {i}: Artist: {t['artist']}; Title: {t['title']}" in body["state"]


def test_the_captured_reply_ranks_the_vocal_house_track_first() -> None:
    fx = _fixture()
    calls: list[dict[str, Any]] = []

    def post(body: dict[str, Any], key: str) -> dict[str, Any]:
        calls.append(body)
        assert key == "test-key"
        return copy.deepcopy(fx["response"])

    out = jev_search.rank(fx["words"], _tracks(fx), key="test-key", model=fx["request"]["model"], post=post)
    assert len(calls) == 1, "one call for the whole shortlist"
    assert set(out["scores"]) == {t["stable_id"] for t in fx["tracks"]}
    assert max(out["scores"], key=out["scores"].__getitem__) == "ai-03"
    assert out["scores"]["ai-05"] < 0.1  # the ambient track does not fit a vocal house sunset set
    assert out["model"] == fx["response"]["model"]
    assert out["cost"] is not None and out["cost"] > 0


def test_no_key_makes_no_call() -> None:
    fx = _fixture()

    def post(body: dict[str, Any], key: str) -> dict[str, Any]:
        raise AssertionError("no call without a key")

    with pytest.raises(jev_search.SearchKeyMissing, match=jev.KEY_ENV):
        jev_search.rank(fx["words"], _tracks(fx), key="  ".strip(), post=post)
    assert jev_search.configured({}) is False
    assert jev_search.configured({jev.KEY_ENV: "  "}) is False
    assert jev_search.configured({jev.KEY_ENV: "k"}) is True


@pytest.mark.parametrize("damage", ["drop", "range", "bool", "no-answers"])
def test_an_answer_missing_any_track_is_an_error_not_a_partial_ranking(damage: str) -> None:
    fx = _fixture()
    doc = copy.deepcopy(fx["response"])
    if damage == "drop":
        del doc["answers"]["t4"]
    elif damage == "range":
        doc["answers"]["t4"]["noul"] = 1.5
    elif damage == "bool":
        doc["answers"]["t4"]["noul"] = True
    else:
        del doc["answers"]
    with pytest.raises(jev_search.SearchFailed):
        jev_search.rank(fx["words"], _tracks(fx), key="k", post=lambda b, k: doc)


def test_a_failed_call_is_an_error() -> None:
    fx = _fixture()

    def post(body: dict[str, Any], key: str) -> dict[str, Any]:
        raise OSError("connection reset")

    with pytest.raises(jev_search.SearchFailed, match="connection reset"):
        jev_search.rank(fx["words"], _tracks(fx), key="k", post=post)


def test_the_request_refuses_empty_words_no_tracks_and_too_many() -> None:
    fx = _fixture()
    with pytest.raises(ValueError):
        jev_search.build_request("  ", _tracks(fx), "m")
    with pytest.raises(ValueError):
        jev_search.build_request("x", [], "m")
    many = [jev_search.SearchTrack(f"s{i}", {"title": "x"}) for i in range(jev_search.MAX_TRACKS + 1)]
    with pytest.raises(ValueError):
        jev_search.build_request("x", many, "m")
    assert jev_search.build_request("x", many[:-1], "m")["questions"]
