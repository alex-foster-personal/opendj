# ratchet_scorecard fixtures

Real GitHub REST responses, captured Thu 15 Oct 2026 with `gh api` from PR #6798, whose head
a8bd6a616 merges origin/main (481646473) onto the branch's own commit 963fa0dc7:

- `compare-327e26688...a8bd6a616.json`: `repos/<repo>/compare/327e266...a8bd6a6`
  (257 commits, 250 returned; 249 of them came in with the main merge).
- `commit-963fa0dc7.json`: `repos/<repo>/commits/963fa0dc7...`.

`MANIFEST.sha256` holds each file's sha256; the tests refuse a file that does not match it.

Trimmed to the fields `scripts/ratchet_scorecard.py` reads (commit `sha`, `parents[].sha`,
`files[].filename`, plus `total_commits`); every value is as GitHub returned it. The full
compare response was 1.5 MB. Re-capture with the two `gh api` paths above and the same `jq`
projection.

## `cmdk-v2-ai/`: one real red-to-green transition, replayed through `collect()`

Captured Fri 16 Oct 2026 with `gh api` from branch `af--cmdk-v2-ai`, window
2026-10-14T20:40Z..22:35Z. ci.yml run 38635253146 on 521027f88 failed only its quality job
(`frontend.max_fan_in: 58 > 56 allowed + 1 slack`) while e2e.yml passed; ci.yml run
38645499936 on 0afb8659e passed, with four cancelled heads in between.

- `runs-ci.json`, `runs-e2e.json`: `actions/workflows/<w>/runs?branch=af--cmdk-v2-ai&created=<window>`,
  trimmed to the fields the scorecard reads. Every other branch's runs in that window are
  left out, so the replay answers a listing with this branch's runs only.
- `jobs-38635253146-1.json`: `actions/runs/38635253146/attempts/1/jobs`, trimmed to id, name,
  conclusion and step name and conclusion.
- `jobs-38645499936-1.json`, `jobs-38645499875-1.json`, `jobs-38635253356-1.json`: the same
  listing for the branch's three successful runs, read because a success on a branch with a
  red can clear it only for the jobs it actually ran (captured Fri 16 Oct 2026).
- `log-116096788066.txt`: `actions/jobs/116096788066/logs`, only its `[quality]` lines.
- `compare-521027f88...0afb8659e.json` and `commit-<sha>.json` for the seven non-merge commits on
  the first-parent chain (`commits/<sha>?per_page=100&page=1`), trimmed as above. Re-capture
  with the same `gh api` paths and projections.

## Merge commits on a first-parent chain (captured Fri 16 Oct 2026)

A merge on the chain from a red head to its fix can carry a conflict resolution, so the scorecard
reads both of its sides: `commits/<merge>?per_page=100&page=<n>` (GitHub lists a merge's files
against its first parent) and `compare/<second parent>...<merge>`. The files in both are counted as
the branch's own work. Captured with those two `gh api` paths, trimmed to `files[].filename`:

- `commit-a8bd6a616.json` and `compare-481646473...a8bd6a616.json` for PR #6798's merge of main.
- In `cmdk-v2-ai/`, `commit-<merge>.json` and `compare-<second parent>...<merge>.json` for each of
  the eight merges on that branch's chain (0afb8659e, 66c2cb3e7, 78f10a133, bcb9badb8, a28159c08,
  6f4049c62, 3d7a6d9f7, c98b2cb3b).

- `compare-<first parent>...<second parent>.json` and the reverse for each of those merges: what
  each side changed since the merge base (`compare` diffs from the merge base), which is where any
  conflict sits however it was resolved. These keep `merge_base_commit.sha` as well.

Each set was checked against `git diff --name-only` from the merge and from the merge base.

## Main ancestry and the head repository (captured Fri 16 Oct 2026)

- `compare-<sha>...main.json`: `repos/<repo>/compare/<sha>...main?per_page=1`, trimmed to `status`,
  for each merge's second parent (481646473 is in main's history, `ahead`; the eight cmdk merges
  merge sibling branch af--cmdk-v2-pairs and are `diverged`), plus b37b5d396 (`diverged`).
- `pulls-481646473.json`: `repos/<repo>/commits/4816464739ef78cc01d678f54999ec91704169dd/pulls?per_page=100`,
  trimmed to `number` and `merged_at`: PR #6838 introduced it to main at 21:21:11Z, before
  a8bd6a616 merged it at 21:23:44Z (captured Sat 17 Oct 2026).
- The merge commits in `compare-327e26688...a8bd6a616.json` and
  `cmdk-v2-ai/compare-521027f88...0afb8659e.json` now also carry `commit.committer.date`, read
  from `repos/<repo>/commits/<sha>` (captured Sat 17 Oct 2026): a merge is judged against main
  as it stood at that time.
- `cmdk-v2-ai/compare-<first parent>...<merge>.json` for the eight chain merges, trimmed to
  `files[].filename`: every file a merge of a non-main branch brought.
- `runs-ci.json` and `runs-e2e.json` now also carry `head_repository.full_name` and `actor.login`,
  read from `actions/runs/<id>` for the same run ids (sha and created_at checked to match).

## main-quality

One passing main push of ci.yml, run 38894149191 at fa5707891 (Fri 16 Oct 2026). Main runs
the quality gate with `--main-report-only`, so its quality job passed while logging
`mypy.errors_tests: 466 > 460 allowed + 5 slack`.

- `main-quality/runs-ci.json`: `actions/runs/38894149191`, trimmed to the fields the scorecard reads.
- `main-quality/jobs-38894149191-1.json`: `actions/runs/38894149191/attempts/1/jobs?per_page=100`,
  trimmed to id, name, conclusion and step names and conclusions.
- `main-quality/log-116950591706.txt`: `actions/jobs/116950591706/logs`, only its `[quality]` lines.
- `main-quality/runs-e2e.json`: `actions/runs/38894149533`, the passing e2e.yml run of the same
  push, trimmed the same way (captured Sat 17 Oct 2026). Its jobs are never requested.

## main-cancelled

Two main pushes of ci.yml on Fri 16 Oct 2026, captured Sat 17 Oct 2026: run 38889649657 at
92b5dd929 was cancelled about a minute after it was created (10:48:31Z), one of twelve cancelled
in a two-minute burst of pushes; run 38889676198 at 9cbfb6527, created 15 s later (10:48:46Z),
passed and logged its quality report (`mypy.errors_tests: 466 > 460 allowed + 5 slack`).

- `main-cancelled/runs-ci.json`: `actions/runs/<id>` for both runs, trimmed to the fields above.
  The other runs of that burst are left out.
- `main-cancelled/jobs-38889676198-1.json`: `actions/runs/38889676198/attempts/1/jobs?per_page=100`,
  trimmed to id, name, conclusion and step names and conclusions.
- `main-cancelled/log-116936195809.txt`: `actions/jobs/116936195809/logs`, only its `[quality]` lines.

## reland-6201

Branch af--reland-6201, ci.yml and e2e.yml run listings for `created=2026-10-16T08:00:00Z..12:00:00Z`
(captured Sat 17 Oct 2026), trimmed to the run fields above. bc6b59d35 (run 38879100684) is red on
pytest; 00159db8c (run 38888830941, attempt 2) passes after it; the remaining runs are cancelled.

- `reland-6201/jobs-<run>-<attempt>.json`: `actions/runs/<run>/attempts/<attempt>/jobs?per_page=100`,
  trimmed to id, name, conclusion and step names and conclusions.
- `reland-6201/log-<job>.txt`: `actions/jobs/<job>/logs`, trimmed to the step echo, exit, selection,
  reason, module, FAILED, ERROR and `##[error]` lines. Each trim was checked to give the same
  shard selection, failed identities, beyond-tests flag and job keys as the full log.
  Job 116958163563 is the passing shard whose selection report the unread-log test withholds.

## main-red

One red main push of ci.yml, run 39004860947 (Sat 17 Oct 2026), captured the same way:
`main-red/runs-ci.json`, `main-red/jobs-39004860947-1.json`, the quality job's log
`main-red/log-117315658214.txt` (only its `[quality]` lines) and the failing job's log
`main-red/log-117315799748.txt` (trimmed as above). The same push's e2e.yml run 39004860989 was
red too (its Trunk jobs failed): `main-red/runs-e2e.json` and `main-red/jobs-39004860989-1.json`.
It is read, and as it has no quality job it must add no report gap.
