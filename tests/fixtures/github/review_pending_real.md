# review_pending_real.json

A real PENDING pull request review, for `tests/scripts/test_review_submitted_state.py`
(REVIEW-12, Codex P1 r4136420652 on PR #4361). The test asserts that an
unsubmitted review carrying a Sol-shaped marker at head is not review evidence,
for review-triage coverage and the enqueue gate's dual-review gate alike.

## How it was made, captured and removed

On Tue 29 Sep 2026, authenticated as the repo owner (the login the Sol and Claude
lanes post under), a throwaway script:

1. read PR #4361's head (`e5969c3589c7368c75ef77157a0f75e0e2f047fb`);
2. created review 5356241811 at 2026-09-29T17:39:00Z with
   `POST repos/<repo>/pulls/4361/reviews`, a body carrying
   `<!-- sol-review v1 sha=<head> seat=test-capture model=none -->` plus a note
   saying it is a test capture, `commit_id` = the head, and NO `event`, so GitHub
   kept it PENDING (visible only to its author, never submitted);
3. read it back with the gate's own read,
   `scripts.review_gh._paginated_json_list("repos/<repo>/pulls/4361/reviews")`, and
   asserted `state == "PENDING"`;
4. deleted it at 2026-09-29T17:39:02Z with
   `DELETE repos/<repo>/pulls/4361/reviews/5356241811` (in a `finally`, so it ran
   whatever the read returned);
5. confirmed the delete with `GET repos/<repo>/pulls/4361/reviews/5356241811`,
   which returned `gh: Not Found (HTTP 404)`. A GET of an existing review on the
   same PR returned 200 in the same session, so the 404 is a real absence.

The raw read of step 3 carried no `submitted_at` key, the real shape of a
PENDING review. The timestamps and the 404 are recorded in the JSON.

## Projection (v2, Tue 29 Sep 2026)

The committed JSON is the raw capture run through
`tests/scripts/enqueue_gate_fixture_projection.py` (`pending` kind): the review
keeps `user.login`, `body`, `commit_id` and `state`, the fields production reads.
Kept values are byte-identical to GitHub's response.

- Raw capture sha256: `ea322d8e59ec8333abe062dee7b46bc20c138982420d889696e47467520d7278`
  (also `_fixture.projected_from_sha256`). The raw file was not committed; its
  extra fields were `_links`, `author_association`, `html_url`, `id`, `node_id`
  and `pull_request_url`.
- Re-project (run from the repository root):
  `python -m tests.scripts.enqueue_gate_fixture_projection pending RAW (private path)`
- `tests/scripts/test_enqueue_gate_fixtures.py` fails if this file is not a fixed
  point of the projection, or if production reads a field the projection dropped.

## Rules

Immutable during feature work. The tests pin this file's sha256
(`PENDING_SHA256` in `tests/scripts/enqueue_gate_helpers.py`). To recapture, repeat
the five steps above (the review must be deleted again and the 404 confirmed),
re-project, update `PENDING_SHA256` in the same commit, and say in the PR body why.
