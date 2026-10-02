# runner_canary captures

`actions_real.json` holds real GitHub Actions payloads from this repository, captured
Tue 29 Sep 2026 20:58Z by the repository owner with
`python -m tests.scripts.runner_canary_fixture_projection capture`, then `project`ed. The
raw capture is not committed. Its sha256 is `_fixture.projected_from_sha256`.

It covers five ci.yml runs:

- 36590440236 and 36590224580: shards 4 and 3 failed on the wall-budget TIMEOUT;
- 36593962700: shard 1 is a real test red;
- 36623327630: a green push run on `main`;
- 36623311438: shard 3 was cancelled in the queue.

`runs` holds the run records. `responses` holds records keyed by the request path the
report issues, selected with the report's own jq. That covers each run's jobs listing
(trimmed to the jobs in `KEEP_JOBS`), every failed shard's annotations, and one template
the report never reads: a passing `CI Insights` job's annotations, which carry a real
titled `::notice`.

**Sanitizing.** Only the fields the report reads are kept, and a shard keeps only its
pytest step. Actors, authors, branches, URLs and runner labels are dropped, not scrubbed.
`runner_name` holds fleet runner hostnames only.

**Mutations.** Vendor shards and the budget gate have no real payload yet. The tests build
them as disclosed mutations of these records on deep copies; see
`tests/scripts/runner_canary_captured.py`. This file is pinned in `MANIFEST.json`.
