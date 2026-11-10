# enqueue_gate_carried_reviews_real.json

Real captured GitHub review payloads for the `carried-findings` gate of
`scripts/enqueue_gate.py`, tested in `tests/scripts/test_enqueue_gate_carried.py`
(ENQGATE-04, Codex P1 r4138704460 on PR #4361,
`(private path)`).

## What is in it

| Key                     | What it holds                                                                  |
| ----------------------- | ------------------------------------------------------------------------------ |
| `prs["4361"].reviews`   | `gh api --paginate --slurp "repos/<repo>/pulls/4361/reviews?per_page=100"`, flattened: all 68 reviews at capture |
| `prs["3827"].reviews`   | `gh api repos/<repo>/pulls/3827/reviews/5303507218`: that ONE review, fetched by id, not the PR's full list |

Why these reviews:

- **#4361, review 5354333330** (Sol, at `fe613ae50`): carried a BLOCKING P1 in
  its body with no inline thread. The real case the finding describes; it must
  refuse.
- **#4361, review 5354209120** (Sol, at `5180648d6`, earlier): a Sol review with
  no carried section. The control; it must not refuse, and as the OLDER review
  it must not supersede 5354333330.
- **#4361, review 5358806344** (Sol, at `b703a66e7`): #4361's latest Sol review
  at capture, carrying nothing. It supersedes 5354333330.
- **#4361, review 5354743421** (the lane login, no lane marker, empty body: a
  thread reply): later than 5354333330, and must not supersede it.
- **#3827, review 5303507218** (Sol, at `932a8a6fb`): a real body carrying only
  NON-BLOCKING findings (one P2, three P3). Found by scanning the reviews of the
  750 most recently created PRs (#3416 to #4513) for the carried header; the
  real P2/P3-only bodies found were 5303507218 (#3827), 5307277014 (#3676) and
  5358732107 (#4034). Nothing in this fixture is an edited body; the one edited
  case (a renamed severity token) is built inside its test and says so.

## Raw capture

Captured 2026-09-29T23:56:02Z to 23:56:03Z (Tue 29 Sep 2026) from the
repository root, authenticated as the repo owner, into a raw document of shape
`{"_fixture": {...}, "prs": {"4361": {"reviews": [...]}, "3827": {"reviews": [...]}}}`
written with `indent=1` plus a newline. Raw sha256:
`8fc8cdf48eee9aa48ec760f5d1af9c0354be47808a377412534900e5bac76c29` (recorded in
the file as `_fixture.projected_from_sha256`).

## Projection (v2)

The committed JSON is the raw capture run through
`tests/scripts/enqueue_gate_fixture_projection.py` (`carried` kind). Each
review keeps `id`, `user.login`, `body`, `state` and `submitted_at`: what
`scripts/enqueue_gate_carried.py` reads. Kept values are byte-identical to
GitHub's response.

- Re-project (run from the repository root):
  `python -m tests.scripts.enqueue_gate_fixture_projection carried RAW (private path)`
- `tests/scripts/test_enqueue_gate_fixtures.py` fails if this file is not a
  fixed point of the projection, if production reads a field the projection
  dropped, or if a field listed as read is not actually read.

## Rules

Immutable during feature work, per `AGENTS.md` -> "No mocks and locked real
fixtures". The tests pin this file's sha256 (`CARRIED_SHA256` in
`tests/scripts/enqueue_gate_helpers.py`), so a hand edit fails loudly.
