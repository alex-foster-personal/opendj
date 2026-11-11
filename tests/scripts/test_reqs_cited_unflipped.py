"""Tests for :mod:`scripts.reqs_cited_unflipped` (issue #1605 step 3).

Regression lines:
  - if a merged PR cites a pending id and it is not reported then broken
  - if a shipped id is reported then broken (the flip is the whole point)
  - if a docs-titled PR is not tagged as such then broken (a reader must be
    able to tell "recorded" from "shipped" at a glance)
  - if gh fails or returns nothing and the tool prints zero then broken (the
    silent-zero the periodic-checks table forbids)
  - if `--reqs-path` on the CLI is ignored in favour of this repo's own
    reqs.json then broken (the periodic-checks step relies on it to test
    against a pinned snapshot instead of live burn-down status)
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts import reqs_cited_unflipped as mod

PAYLOAD = {
    "v1": {
        "OPS": {
            "name": "Ops",
            "requirements": [
                {"id": "OPS-21", "desc": "x", "status": "shipped"},
                {"id": "OPS-22", "desc": "x", "status": "pending"},
                {"id": "OPS-2", "desc": "x", "status": "pending"},
            ],
        }
    }
}
# Shape of `gh pr list --json number,title,body,mergedAt`; captured, not invented.
PRS = [
    {
        "number": 1387,
        "title": "feat(ops): worktree lifecycle (OPS-21, OPS-22)",
        "body": "guard and reaper",
        "mergedAt": "2026-09-06T10:00:00Z",
    },
    {
        "number": 826,
        "title": "docs(requirements): record OPS-12..17",
        "body": "mentions OPS-22 in prose",
        "mergedAt": "2026-09-02T10:00:00Z",
    },
    {
        "number": 999,
        "title": "chore: unrelated",
        "body": "no ids here",
        "mergedAt": "2026-09-03T10:00:00Z",
    },
]


def test_cited_pending_is_reported_and_shipped_is_not() -> None:
    found = mod.citations(mod.pending_v1_ids(PAYLOAD), PRS)
    ids = {c.req_id for c in found}
    assert "OPS-22" in ids, "a merged PR cites OPS-22 and it is still pending"
    assert "OPS-21" not in ids, "OPS-21 is shipped; reporting it defeats the flip"


def test_word_boundary_stops_prefix_matches() -> None:
    # OPS-2 is a prefix of OPS-21 and OPS-22; neither PR cites OPS-2 itself.
    found = mod.citations(mod.pending_v1_ids(PAYLOAD), PRS)
    assert not [c for c in found if c.req_id == "OPS-2"], "prefix matched a longer id"


def test_docs_titled_prs_are_tagged() -> None:
    found = {(c.pr_number, c.docs_only_title) for c in mod.citations(["OPS-22"], PRS)}
    assert (826, True) in found and (1387, False) in found


def test_unreadable_source_is_unknown_not_zero(tmp_path: Path, capsys) -> None:
    reqs = tmp_path / "reqs.json"
    reqs.write_text(json.dumps(PAYLOAD))

    def boom(_window: str) -> list[dict]:
        raise RuntimeError("gh: no auth")

    rc = mod.main([], fetch=boom, reqs_path=reqs)
    err = capsys.readouterr().err
    assert rc == 2 and "UNKNOWN" in err, "a failed read rendered as a result"

    rc = mod.main([], fetch=lambda days: [], reqs_path=reqs)
    err = capsys.readouterr().err
    assert rc == 2 and "UNKNOWN" in err, "an empty read rendered as zero cited"


def test_strict_exits_one_only_when_found(tmp_path: Path, capsys) -> None:
    reqs = tmp_path / "reqs.json"
    reqs.write_text(json.dumps(PAYLOAD))
    assert mod.main(["--strict"], fetch=lambda d: PRS, reqs_path=reqs) == 1
    out = capsys.readouterr().out
    assert "OPS-22" in out and "#1387" in out and "[docs-title]" in out
    assert mod.main(["--strict"], fetch=lambda d: PRS[2:], reqs_path=reqs) == 0


def test_reqs_path_flag_overrides_the_default(tmp_path: Path, capsys) -> None:
    """The CLI flag the periodic-checks step uses to read a pinned snapshot.

    Exercised through argv, not the `reqs_path=` keyword the other tests use:
    that keyword is production's own default-override for callers of `main()`
    directly, but the workflow step only ever gets argv, via `--reqs-path`.
    Passing a `reqs_path=` keyword here too would prove the keyword works, not
    that the flag parses into it.
    """
    reqs = tmp_path / "custom_reqs.json"
    reqs.write_text(json.dumps(PAYLOAD))
    rc = mod.main(["--reqs-path", str(reqs), "--strict"], fetch=lambda d: PRS)
    out = capsys.readouterr().out
    assert rc == 1 and "OPS-22" in out, out


# A recorded window spans two instants, and gh's `merged:` range is
# DAY-granular: the term is the whole of both days and the exact ends are
# applied to what comes back. These three PRs sit on the boundary the term
# cannot express, so they are what separates the two behaviours.
WINDOW_SINCE = "2026-09-05T12:00:00Z"
WINDOW_UNTIL = "2026-09-06T12:00:00Z"
BOUNDARY_PRS = [
    {"number": 1, "title": "t", "body": "OPS-22", "mergedAt": "2026-09-05T00:30:00Z"},
    {"number": 2, "title": "t", "body": "OPS-22", "mergedAt": "2026-09-05T12:30:00Z"},
    {"number": 3, "title": "t", "body": "OPS-22", "mergedAt": "2026-09-06T23:30:00Z"},
]


def test_recorded_interval_asks_for_both_whole_days() -> None:
    window = mod.window_bounds(None, WINDOW_SINCE, WINDOW_UNTIL)
    assert window.search == "2026-09-05..2026-09-06", (
        "the gh search term must cover both recorded days in full: gh cannot "
        f"express the intrabar bounds (got {window.search!r})"
    )


def test_recorded_interval_excludes_merges_outside_its_exact_ends(tmp_path: Path, capsys) -> None:
    """The whole point of #1605's review: the recorded ends are the bound.

    PR 1 merges INTO the window's first day from before it, PR 3 lands after the
    recorded head; both fall inside the day-granular search term and neither is
    in the interval the window measured. PR 2 is the only one that is.
    """
    reqs = tmp_path / "reqs.json"
    reqs.write_text(json.dumps(PAYLOAD))
    seen: list[str] = []

    def fetch(search: str) -> list[dict]:
        seen.append(search)
        return BOUNDARY_PRS

    rc = mod.main(["--since", WINDOW_SINCE, "--until", WINDOW_UNTIL], fetch=fetch, reqs_path=reqs)
    out = capsys.readouterr().out
    assert seen == ["2026-09-05..2026-09-06"], seen
    assert rc == 0, out
    assert "#2" in out, f"the merge inside the interval was dropped:\n{out}"
    assert "#1" not in out and "#3" not in out, f"a merge outside the recorded ends was reported as in-window:\n{out}"


def test_a_read_that_succeeds_and_finds_nothing_is_a_real_zero(tmp_path: Path, capsys) -> None:
    """An empty WINDOW is a measurement; an empty READ is not.

    The raw read is non-empty here, so `gh` answered -- there is simply nothing
    merged inside the recorded interval. That must render as zero, not as the
    UNKNOWN a failed read reserves, or a quiet window would red the periodic
    tier every time it ran.
    """
    reqs = tmp_path / "reqs.json"
    reqs.write_text(json.dumps(PAYLOAD))
    outside = [{"number": 9, "title": "t", "body": "OPS-22", "mergedAt": "2026-08-01T00:00:00Z"}]

    rc = mod.main(
        ["--since", WINDOW_SINCE, "--until", WINDOW_UNTIL],
        fetch=lambda search: outside,
        reqs_path=reqs,
    )
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert "cited pending ids=0" in captured.out, captured.out
    assert "UNKNOWN" not in captured.err, captured.err
