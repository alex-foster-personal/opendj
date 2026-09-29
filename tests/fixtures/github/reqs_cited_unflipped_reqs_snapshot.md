# reqs_cited_unflipped_reqs_snapshot.json

A pinned, minimal `reqs.json` payload for
`tests/quality/test_periodic_checks_reqs_drift_row.py`. It supplies the
`v1` pending-id side of `test_a_cited_pending_id_is_reported`, the same way
`reqs_cited_unflipped_pr_list.json` supplies the merged-PR side.

## Why this exists (the class of bug, not a one-off)

`_expected_cited_ids()` used to intersect the captured PR list against the
LIVE `reqs.json`'s pending v1 ids. As v1 burns down, every id the captured
window cites eventually flips to `shipped`, and the test then asserts against
an empty set and fails with "cites no pending id any more" -- not because
anything regressed, but because the last of the three ids the window cited
(`DEVOPS-07`, `NATIVE-05`, `NATIVE-13`) flipped or moved out of `v1`. This
will recur for the next PR list capture too unless the pending side is also
pinned, which is what this file does.

## What is in it

The minimal shape `scripts.reqs_cited_unflipped.pending_v1_ids` needs:
`{"v1": {<section>: {"requirements": [{"id", "status", ...}]}}}`. It holds
only the `DEVOPS` ids that `reqs_cited_unflipped_pr_list.json`'s Wed 9 Sep
2026 window (`merged:2026-09-09..2026-09-09`) actually cites under that
section (`DEVOPS-01`, `DEVOPS-06`, `DEVOPS-07`) -- not the full `reqs.json`.

`DEVOPS-01` and `DEVOPS-06` are pinned `shipped`, matching their real status.
`DEVOPS-07` is pinned `pending`, which is also its REAL status as of the
capture date: it shipped later, Sat 26 Sep 2026 (see its own `desc` in
`reqs.json`). Nothing here is fabricated; it is reqs.json's own Wed 9 Sep
2026 state for these three ids, narrowed to what the test needs.

The window's other two pending citations at capture time, `NATIVE-05` and
`NATIVE-13`, are not included: `NATIVE-05` has since moved to `v2` (this
lane's own PR, #4370) and `NATIVE-13` is gone from `v1`/`v2`/`out_of_scope`
entirely, so replicating them as `v1` entries would misrepresent the
schema rather than pin a real historical state. One pinned pending id
(`DEVOPS-07`) is sufficient to keep the test non-vacuous.

## How production reads it

`scripts/reqs_cited_unflipped.py`'s `--reqs-path` flag points the tool at
any `reqs.json`-shaped payload; it defaults to this repo's own file. The
`periodic-checks.yml` `reqs-drift` step reads `$REQS_JSON_PATH` and, only
when that env var is set, adds `--reqs-path "$REQS_JSON_PATH"` to its
argv -- unset in production, so live runs are unaffected. The test sets it
to this file's path so the production `run:` block under test reads the
snapshot instead of whatever `reqs.json` currently says.

## Keeping it non-vacuous

`tests/quality/test_periodic_checks_reqs_drift_row.py::test_the_snapshot_is_not_vacuous`
asserts this file still has a pending citation, then mutates an in-memory
copy (every requirement flipped to `shipped`) and asserts THAT reads back
to an empty citation set -- proving the assertion actually bites rather than
passing for an unrelated reason. If a future edit to this file ever removes
its last pending id, that test goes red with the same message
`test_a_cited_pending_id_is_reported` used to give, and the fix is to pin a
different still-relevant id here, not to re-widen the intersection back to
live `reqs.json`.

## Immutability

Not sha256-pinned like the PR-list cassette: unlike that capture, this file
is the fixture's own authored state, not a replay of an external response,
so hand-editing it is the intended way to re-pin a different id when
`DEVOPS-07` (or whichever id is pinned) itself needs to change. Keep the
"why" note above in sync with whichever id is pinned pending.
