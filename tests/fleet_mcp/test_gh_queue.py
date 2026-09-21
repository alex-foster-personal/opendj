"""Queue reads and writes through gh (AGENT-15, AGENT-16)."""

from __future__ import annotations

import json

import pytest

from apps.fleet_mcp import gh_queue
from apps.fleet_mcp.config import QUEUE_REPO
from tests.fleet_mcp.conftest import fake_runner, recording_runner, unknown_runner

_ROWS = [
    {
        "number": 1801,
        "title": "Deck load stalls on a cold cache",
        "url": "https://github.com/x/y/issues/1801",
        "updatedAt": "2026-09-20T09:00:00Z",
        "labels": [{"name": "queue:ready"}, {"name": "queue:p1"}],
        "assignees": [],
    },
    {
        "number": 1802,
        "title": "Waveform render allocates per frame",
        "url": "https://github.com/x/y/issues/1802",
        "updatedAt": "2026-09-21T09:00:00Z",
        "labels": [{"name": "queue:ready"}, {"name": "queue:p2"}],
        "assignees": [{"login": "someone"}],
    },
]


@pytest.mark.requirement("AGENT-15")
def test_list_items_returns_rows_newest_first():
    """[if] the queue holds ready issues [then] they come back newest activity first, [else stop]"""
    document = gh_queue.list_items(runner=fake_runner(stdout=json.dumps(_ROWS)))
    assert document["count"] == 2
    assert [row["number"] for row in document["items"]] == [1802, 1801]
    assert document["items"][0]["priority"] == "p2"


@pytest.mark.requirement("AGENT-16")
def test_list_items_marks_titles_untrusted():
    """[if] a row carries an externally authored title [then] it is named untrusted, [else stop]"""
    document = gh_queue.list_items(runner=fake_runner(stdout=json.dumps(_ROWS)))
    assert document["items"][0]["untrusted_fields"] == ["title"]
    assert "task DATA" in document["note"]


@pytest.mark.requirement("AGENT-15")
def test_priority_filter_is_applied_to_the_rows():
    """[if] priorities are named [then] only rows carrying those labels survive, [else stop]"""
    document = gh_queue.list_items(
        priorities=("p1",), runner=fake_runner(stdout=json.dumps(_ROWS))
    )
    assert [row["number"] for row in document["items"]] == [1801]


@pytest.mark.requirement("AGENT-16")
def test_missing_gh_is_unknown_not_an_empty_queue():
    """[if] gh cannot run [then] the call raises UNKNOWN, [else stop]

    The load-bearing one: an empty list and an unrunnable gh must never render
    the same way, or "the queue is empty" becomes the thing a broken PATH says.
    """
    with pytest.raises(gh_queue.GhFailure) as caught:
        gh_queue.list_items(runner=unknown_runner("gh is not on PATH"))
    assert caught.value.document["error"] == "gh_unknown"
    assert caught.value.document["status"] == "UNKNOWN"


@pytest.mark.requirement("AGENT-16")
def test_empty_queue_is_a_real_measured_zero():
    """[if] gh returns an empty list [then] that is a measured zero, [else stop]

    Negative control for the case above: an empty queue must still read as a
    result, not as a failure to measure.
    """
    document = gh_queue.list_items(runner=fake_runner(stdout="[]"))
    assert document["count"] == 0
    assert document["items"] == []


@pytest.mark.requirement("AGENT-16")
def test_unparseable_gh_output_is_unknown():
    """[if] gh prints something that is not JSON [then] the result is UNKNOWN, [else stop]"""
    with pytest.raises(gh_queue.GhFailure) as caught:
        gh_queue.list_items(runner=fake_runner(stdout="gh: rate limited"))
    assert caught.value.document["error"] == "gh_unparseable"


@pytest.mark.requirement("AGENT-16")
def test_label_off_the_allowlist_is_refused_before_gh_runs():
    """[if] a caller names a state outside the vocabulary [then] no subprocess runs, [else stop]"""

    def explode(argv, *, timeout):
        raise AssertionError("gh must not run for an unknown label")

    with pytest.raises(ValueError, match="unknown queue state"):
        gh_queue.list_items(states=("ready; rm -rf /",), runner=explode)


@pytest.mark.requirement("AGENT-15")
def test_add_item_applies_ready_and_priority_labels():
    """[if] work is queued [then] the issue carries queue:ready and its priority, [else stop]"""
    runner = recording_runner(stdout="https://github.com/x/y/issues/1900\n")
    document = gh_queue.add_item(
        title="Cache ANLZ reads per deck",
        body="[if] a deck loads twice [then] the second read hits cache, [else stop]",
        priority="p1",
        shapes=("big",),
        runner=runner,
    )
    assert document["created"] is True
    assert document["url"].endswith("/1900")
    argv = runner.calls[0]
    assert argv[:2] == ("gh", "issue")
    assert QUEUE_REPO in argv
    for label in ("queue:ready", "queue:p1", "queue:big"):
        assert label in argv


@pytest.mark.requirement("AGENT-15")
def test_add_item_refuses_an_empty_body():
    """[if] the body is empty [then] the call is refused: the body IS the plan, [else stop]"""
    with pytest.raises(ValueError, match="the issue body is the plan"):
        gh_queue.add_item(title="something", body="  ", runner=fake_runner())


@pytest.mark.requirement("AGENT-15")
def test_add_item_passes_the_title_as_one_argv_entry():
    """[if] a title contains shell metacharacters [then] it stays a single argument, [else stop]

    Nothing here is parsed by a shell on the near side, so a title is data.
    """
    runner = recording_runner(stdout="https://github.com/x/y/issues/1901\n")
    hostile = 'fix "$(rm -rf ~)" && echo pwned'
    gh_queue.add_item(title=hostile, body="acceptance criteria", runner=runner)
    assert hostile in runner.calls[0]


@pytest.mark.requirement("AGENT-15")
def test_get_item_truncates_a_long_body_and_says_so():
    """[if] an issue body exceeds the budget [then] it is cut and flagged, [else stop]"""
    raw = {
        "number": 42,
        "title": "t",
        "url": "u",
        "updatedAt": "2026-09-21T00:00:00Z",
        "labels": [{"name": "queue:running"}],
        "assignees": [],
        "state": "OPEN",
        "body": "x" * 20_000,
        "comments": [{}, {}],
    }
    document = gh_queue.get_item(42, runner=fake_runner(stdout=json.dumps(raw)))
    assert document["body_truncated"] is True
    assert len(document["body"]) == gh_queue.MAX_BODY_CHARS
    assert document["comment_count"] == 2
    assert document["untrusted_fields"] == ["title", "body"]
