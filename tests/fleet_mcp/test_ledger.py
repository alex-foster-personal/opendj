"""Ledger-as-lock behavior for the dispatch connector (AGENT-15, AGENT-16).

The lock's whole value is that it refuses. So the suite drives BOTH directions
of every gate: the claim that must be allowed, and the claim that must not be,
plus the overshoot case where takeover is enabled but unexplained.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from apps.fleet_mcp import ledger
from apps.fleet_mcp.config import ENABLE_TAKEOVER_ENV

_ETAG = '"abc123"'

#: Any resolvable-looking sha: these tests exercise the lease branching, and
#: the transport is a stand-in, so the value is never resolved. The REAL
#: contract is covered against the real route in test_ledger_contract.py.
_SHA = "0" * 40


def _stamp(hours_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")


def _tree(node: dict) -> dict:
    return {"areas": [{"id": "perf", "nodes": [node]}], "rollups": {"perf": {}}}


def _client(node: dict, *, patch_status: int = 200, sent: list | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_tree(node), headers={"ETag": _ETAG})
        if sent is not None:
            sent.append(request)
        return httpx.Response(patch_status, json={"node": {**node, "status": "building"}})

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://ledger.test")


@pytest.fixture(autouse=True)
def _origin(monkeypatch):
    monkeypatch.setattr(ledger, "base_url", lambda: "http://ledger.test")


@pytest.mark.requirement("AGENT-15")
def test_claim_of_an_unheld_node_patches_with_the_etag():
    """[if] a node is unclaimed [then] one call reads, pins the ETag and PATCHes it, [else stop]"""
    sent: list[httpx.Request] = []
    node = {"id": "perf-01", "title": "Deck load", "status": "missing"}
    document = ledger.claim(
        "perf-01",
        branch="af--deck-load",
        commit_sha=_SHA,
        worktree="../music-dj-tools-wt-deck-load",
        client=_client(node, sent=sent),
    )
    assert document["claimed"] is True
    assert document["took_over"] is False
    assert sent[0].headers["If-Match"] == _ETAG
    body = json.loads(sent[0].read())
    assert body["status"] == "building"
    assert body["build"] == {
        "branch": "af--deck-load",
        # "active", not "building": build.state describes the WORK and the route
        # only accepts active|idle|blocked|hanging. "building" is the NODE's
        # status and was rejected outright (#3735).
        "state": "active",
        "worktree": "../music-dj-tools-wt-deck-load",
    }
    # The route refuses a status change that cites no commit, so the claim must
    # carry one or it never lands. Covered against the real route in
    # test_ledger_contract.py; asserted here so the payload shape cannot drift.
    assert body["commits_append"] == [
        {"sha": _SHA, "note": "claimed for af--deck-load"}
    ]


@pytest.mark.requirement("AGENT-16")
def test_claim_refuses_a_node_under_a_live_lease():
    """[if] another agent holds a live lease [then] the claim is refused, [else stop]"""
    node = {
        "id": "perf-01",
        "status": "building",
        "build": {"branch": "af--someone-else", "updated": _stamp(0.5)},
    }
    with pytest.raises(ledger.LedgerFailure) as caught:
        ledger.claim(
            "perf-01",
            branch="af--mine",
            commit_sha=_SHA,
            worktree="wt",
            client=_client(node),
        )
    document = caught.value.document
    assert document["error"] == "claim_held"
    assert "af--someone-else" in document["message"]
    assert ENABLE_TAKEOVER_ENV in document["remedy"]


@pytest.mark.requirement("AGENT-15")
def test_a_stale_lease_is_claimable_without_takeover():
    """Control for the opposite direction: a lapsed lease must NOT stay locked.

    [if] the holder's lease is older than the lease window [then] the node is, [else stop]
    claimable, or a dead agent would hold a feature forever.
    """
    node = {
        "id": "perf-01",
        "status": "building",
        "build": {"branch": "af--abandoned", "updated": _stamp(ledger.LEASE_HOURS + 1)},
    }
    document = ledger.claim(
            "perf-01",
            branch="af--mine",
            commit_sha=_SHA,
            worktree="wt",
            client=_client(node),
        )
    assert document["claimed"] is True
    assert document["took_over"] is False


@pytest.mark.requirement("AGENT-16")
def test_takeover_env_alone_does_not_permit_a_silent_takeover(monkeypatch):
    """[if] takeover is enabled but no note is given [then] the claim is still refused, [else stop]

    The overshoot case: enabling takeover must not turn the lock off, only
    make an EXPLAINED takeover possible.
    """
    monkeypatch.setenv(ENABLE_TAKEOVER_ENV, "1")
    node = {
        "id": "perf-01",
        "status": "building",
        "build": {"branch": "af--live", "updated": _stamp(0.1)},
    }
    with pytest.raises(ledger.LedgerFailure) as caught:
        ledger.claim(
            "perf-01",
            branch="af--mine",
            commit_sha=_SHA,
            worktree="wt",
            client=_client(node),
        )
    assert caught.value.document["error"] == "claim_held"


@pytest.mark.requirement("AGENT-16")
def test_explained_takeover_is_allowed_and_flagged(monkeypatch):
    """[if] takeover is enabled AND a note is given [then] the claim lands, flagged, [else stop]"""
    monkeypatch.setenv(ENABLE_TAKEOVER_ENV, "1")
    node = {
        "id": "perf-01",
        "status": "building",
        "build": {"branch": "af--live", "updated": _stamp(0.1)},
    }
    document = ledger.claim(
        "perf-01",
        branch="af--mine",
        commit_sha=_SHA,
        worktree="wt",
        note="worker job-1801 died with the WSL restart at 09:12Z",
        client=_client(node),
    )
    assert document["claimed"] is True
    assert document["took_over"] is True


@pytest.mark.requirement("AGENT-16")
def test_unstamped_lease_counts_as_live():
    """[if] build.updated is missing [then] the claim is treated as live, not stale, [else stop]

    An unmeasured lease is not an expired one; failing open here would let an
    unstamped claim be stolen.
    """
    node = {"id": "perf-01", "status": "building", "build": {"branch": "af--live"}}
    with pytest.raises(ledger.LedgerFailure) as caught:
        ledger.claim(
            "perf-01",
            branch="af--mine",
            commit_sha=_SHA,
            worktree="wt",
            client=_client(node),
        )
    assert caught.value.document["error"] == "claim_held"
    assert caught.value.document["lease_age_hours"] is None


@pytest.mark.requirement("AGENT-15")
def test_a_stale_etag_is_reported_as_retryable():
    """[if] the ledger moved between read and PATCH [then] retry is advised, [else stop]"""
    node = {"id": "perf-01", "status": "missing"}
    with pytest.raises(ledger.LedgerFailure) as caught:
        ledger.claim(
            "perf-01",
            branch="af--mine",
            commit_sha=_SHA,
            worktree="wt",
            client=_client(node, patch_status=412),
        )
    assert caught.value.document["error"] == "etag_stale"


@pytest.mark.requirement("AGENT-15")
def test_claim_requires_somewhere_for_the_work_to_live():
    """[if] neither worktree nor pr is given [then] the claim is refused, [else stop]"""
    node = {"id": "perf-01", "status": "missing"}
    with pytest.raises(ValueError, match="worktree or pr"):
        ledger.claim("perf-01", branch="af--mine", commit_sha=_SHA, client=_client(node))


@pytest.mark.requirement("AGENT-15")
def test_read_returns_one_node_with_its_lease_age():
    """[if] a node id is given [then] its record and lease age come back, [else stop]"""
    node = {"id": "perf-01", "status": "building", "build": {"updated": _stamp(2)}}
    document = ledger.read("perf-01", client=_client(node))
    assert document["node"]["id"] == "perf-01"
    assert 1.9 < document["lease_age_hours"] < 2.1


@pytest.mark.requirement("AGENT-15")
def test_read_of_a_missing_node_is_an_error_not_an_empty_document():
    """[if] the id names no node [then] the call fails loudly, [else stop]"""
    node = {"id": "perf-01", "status": "missing"}
    with pytest.raises(ledger.LedgerFailure) as caught:
        ledger.read("perf-99", client=_client(node))
    assert caught.value.document["error"] == "node_not_found"
