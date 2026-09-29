# runner_canary captures

`actions_real.json` holds real GitHub Actions payloads from this repository. The
runner-canary report tests (`tests/scripts/test_runner_canary_report.py`,
`test_runner_canary_rows.py`, `test_runner_canary_fixtures.py`) replay it through the
report's own read path, `scripts.runner_canary_report._with_jobs`.

## What is in it

- `runs`: the run records for these five ci.yml runs, captured with
  `gh api repos/<repo>/actions/runs/<id>`:
  - 36590440236 and 36590224580, whose shards 4 and 3 failed on the wall-budget TIMEOUT;
  - 36593962700, whose shards 1 to 3 were real test reds;
  - 36623327630, a green push run on `main`;
  - 36623311438, a push run whose shards were cancelled in the queue.
- `responses`: records keyed by the exact request path the report issues. Each was selected
  with the report's own `--jq`:
  - the jobs listing of each run (`.jobs[]`), keeping only the jobs named in
    `_fixture.kept_jobs`, to fit the byte budget;
  - the check-run annotations of every failed shard (`.[]`);
  - the annotations of one passing job, `CI Insights`, which carries a real titled
    `::notice`. The report never reads these. The scenario builders start the budget
    gate's shape from them.

It was captured Tue 29 Sep 2026 at 20:58Z, as the repository owner, with
`python -m tests.scripts.runner_canary_fixture_projection capture`. It was projected with
`... project`. The raw capture is not committed; its sha256 is `_fixture.projected_from_sha256`.

## Sanitizing

The projection keeps only the fields the report reads, so everything else is dropped rather
than scrubbed. That covers actors, commit authors, branch names, URLs, runner labels and
groups, and step numbers and timings. `runner_name` is kept because the report reads it,
and it holds only fleet runner hostnames.

## What is real and what is a disclosed mutation

Every job and annotation here is real. No canary run has happened yet, so every vendor
shard and budget-gate job the tests use is a disclosed mutation of one of these records.
`tests/scripts/runner_canary_captured.py` lists each mutation, and it mutates deep copies
only. This file is pinned in `MANIFEST.json` and verified before it is parsed.
