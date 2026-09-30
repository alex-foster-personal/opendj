# Contributing to music-dj-tools

Thanks for taking the time to contribute. This doc is the short list of
rules that keep the project coherent across many sub-agent and human
contributors. Read it once; the conventions are load-bearing.

## Table of contents

1. [Project structure](#project-structure)
2. [Dev setup](#dev-setup)
3. [Running tests and lint](#running-tests-and-lint)
4. [Branching convention](#branching-convention)
5. [Conventional commits](#conventional-commits)
6. [The six-rail safety pattern](#the-six-rail-safety-pattern)
7. [Proposing a new phase](#proposing-a-new-phase)
8. [House rules](#house-rules)
9. [Developer Certificate of Origin](#developer-certificate-of-origin)
10. [Licensing](#licensing)

## Project structure

The repo is a flat `apps/` monorepo. High-level map (see
[`README.md`](README.md) for the exhaustive version):

```
apps/
  shared/        paths, DB wrappers, Phase 5 shared state
  sync/          Rekordbox <-> djay playlist / cue / beatgrid / rating sync
  audit/         reconcile, sync-diff, cue-comparison tooling
  analysis/      librosa + madmom + MIK backends
  dedup/         chromaprint dedup + cluster apply
  tags/          tag unification + ID3 / MP4 / Vorbis writer
  smartlists/    rule engine + materialiser
  spotify/       Spotify importer + rematch
  cloud/         Litestream + R2 lock + Syncthing fallback
  voice/         wake-word + grammar loop
  webui/         FastAPI daemon + SvelteKit UI
  launcher/      Tauri cmd-K launcher
open-dj/         cross-format metadata spec (v0.2)
docs/            operator-facing docs
.planning/       phase docs, orchestration playbooks, review queues
tests/           pytest suites; fixtures under tests/fixtures/
scripts/         one-off tools, build helpers, fixture builders
```

## Dev setup

Python 3.11 is the pinned floor. Local dev on 3.14 works for non-voice
features; see [`docs/operator-setup.md`](docs/operator-setup.md) for the
full story and for feature-specific system deps (ffmpeg, chromaprint,
etc.).

```bash
cd /path/to/music-dj-tools
python3 -m venv .venv
source .venv/bin/activate
pip install --no-build-isolation -r requirements.txt
python -m pyrekordbox download-key   # one-time: cache master.db decryption key
```

`--no-build-isolation` is required because `madmom`'s own build-requires ask
for `numpy>2`, while this venv pins `numpy<2`; building against the ambient
venv keeps the compiled extensions on the numpy that is actually installed.

## Running tests and lint

```bash
make test         # pytest -q over the whole suite
make cov          # adds coverage (term + HTML report in htmlcov/)
make integration  # pytest -m integration (slower, disk-touching)
make reqs-check   # verify requirements.txt matches reqs.json lock
make ci           # what CI runs: reqs-check + cov
make fixture      # rebuild the Rekordbox test fixture DB
```

Lint and formatting tools are not yet wired into `make`. Match the
existing code style; CI will grow ruff + mypy incrementally. PRs that
reformat unrelated code will be asked to split that out.

Optional local linting via [Trunk](https://docs.trunk.io/code-quality) (config in
`.trunk/trunk.yaml`, not a merge gate; `just quality` stays authoritative):

```bash
npm ci            # once: installs the trunk launcher into ./node_modules
npm run lint      # trunk check --no-fix: new issues on lines changed vs main only, read-only
npm run lint:fix  # trunk check with autofix, for interactive use
npm run fmt       # trunk fmt: changed files only; near no-op today (see below)
```

`npm run fmt` currently has only dotenv-linter to run: black, prettier, ruff format,
shfmt, taplo and rustfmt are disabled because none is an existing gate and each
rewrites whole files. Enabling one is a separate decision.

Trunk's git hooks are disabled on purpose (shared checkouts); run it on demand.
Without a TTY (agents, CI) a bare `trunk check` applies autofixes without asking, so
`npm run lint` passes `--no-fix`; run `npm run lint:fix` when you want the fixes applied.

## Branching convention

Starting with the v1.1 cycle, no change lands on `master` directly.
Every change, including one-line docs tweaks, goes through a pull
request from a feature, seed, fix, or docs branch:

```
feat/<slug>     # new feature or phase
fix/<slug>      # non-trivial bug fix
seed/<seed-id>  # exploratory branch spawned from .planning/seeds/
docs/<slug>     # docs only
chore/<slug>    # tooling, CI, dependency bumps
refactor/<slug> # behaviour-preserving code move
test/<slug>     # tests only
```

v1 and earlier work lived on `master` with no PR trail. That history is
kept as-is; the milestone tags (`v1.0-rc*`, `v1.0`) are the audit
record. The convention starts from v1.1.

Server-side branch protection is not enabled (this repo is private on
GitHub Free, which does not support the protection API). The client
side stand-in is the pre-push hook at
[`scripts/githooks/pre-push-master-guard.sh`](scripts/githooks/pre-push-master-guard.sh).
Install it once after cloning:

```bash
ln -sf ../../scripts/githooks/pre-push-master-guard.sh .git/hooks/pre-push
```

Reviewer lease enforcement (issue #272) uses a separate optional hook that refuses
pushes when another fleet holds `reviewer:codex` or `reviewer:claude` on the open
PR for that branch. Install it instead of, or chained with, the master guard:

```bash
ln -sf ../../scripts/githooks/pre-push-reviewer-lease.sh .git/hooks/pre-push
```

If both hooks are needed, call each from one dispatcher script. See
`scripts/review_lease.py` and `.planning/FANOUT-CONVENTIONS.md`.

The full rationale, grammar, and lifecycle are in
[`docs/branching.md`](docs/branching.md).

## Conventional commits

Every commit message follows
[Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short summary>

[optional body]

[optional footer, including Signed-off-by]
```

Common types: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `build`,
`ci`. Scopes are usually a phase number (`feat(7):`), an app module
(`fix(sync):`), or a governance area (`docs(governance):`).

## The six-rail safety pattern

Every destructive write path in this repo goes through the six-rail safety
pattern. If you add or modify a code path that writes to Rekordbox, djay,
Serato, Traktor, or any operator file on disk, all six rails apply. See
[`apps/sync/safety.py`](apps/sync/safety.py) for the canonical helpers and
the Safety section of [`README.md`](README.md) for the summary.

1. **Typed confirmation.** Operator must type a non-trivial string (not
   just "y"). Use the helpers in `apps/sync/safety.py`.
2. **Running-app check.** `pgrep` for Rekordbox / djay / Serato /
   Traktor and abort if the target app is running. Catch the lock before
   you touch the file. A missing or erroring `pgrep` is now a hard
   error (PR #81); the CLI override `--force-no-pgrep` exists only for
   operators who have manually confirmed the vendor app is quit. Test
   code may pass `allow_app_running=True` into `apps/tags/apply.py`,
   but the `_in_pytest()` guard rejects that kwarg anywhere outside a
   pytest run (PR #86).
3. **Timestamped backup.** Copy the target file to a timestamped path
   before the write. Every rail-3 backup must be restorable by the
   reversal script in rail 6.
4. **Dry-run default.** A live write needs both `--live` and
   `--i-understand-the-risks`. Dry-run output must be the diff the live
   run will apply, not a summary.
5. **Atomic write.** Write to a temp file in the same directory, then
   rename. Never truncate-in-place.
6. **Post-write readback plus reversal script.** Re-open the written file
   and verify the change took; emit a reversal script that restores the
   rail-3 backup. A new writer without a reversal script will not be
   merged. For DB writers, run the readback inside the same
   `BEGIN IMMEDIATE` / `COMMIT` as the write so a mismatch can
   `ROLLBACK` before anything becomes durable (`apps/sync/playlist_apply.py`,
   PR #81 is the reference implementation).

Parallel fail-closed rule for cloud write guards: if a peer-lock probe
raises, the FastAPI dependency must return `503 lock_probe_failed`
rather than defaulting to "no peer holds the lock"
(`apps/webui/server/deps.py:get_lock_status`, PR #85). The lock itself
must be held until the Litestream subprocess has actually exited
(`apps/cloud/replicate.py:_wait_for_proc_exit`).

Voice intents that are destructive are additionally gated behind
`--enable-destructive`. New destructive CLI surfaces should follow the
same pattern.

## Proposing a new phase

Non-trivial features land as numbered phases, not as ad-hoc branches.
If you want to propose one:

1. Skim
   [`.planning/orchestration/gsd-orchestrator-playbook.md`](.planning/orchestration/gsd-orchestrator-playbook.md)
   for the phase lifecycle (discuss -> plan -> execute -> review -> UAT).
2. Open an issue titled `phase(proposal): <slug>` with: one-paragraph
   scope, the safety rails it touches, the expected test fixtures, and a
   rough exit criterion.
3. For smaller changes, just open a PR. The issue-first rule is only for
   changes that want a new phase folder under `.planning/phases/`.

Existing phase docs under `.planning/phases/NN-slug/` are the best
reference for the level of detail expected.

## House rules

These are load-bearing conventions enforced by reviewers and sub-agents.
Violations are the single most common reason a PR gets sent back.

For a pytest `@pytest.mark.requirement("<ID>")` TEST disposition, put one
single-line intent in the marked module's docstring, or in the marked test
function's docstring: `[if] X [then] Y, [else stop]`. For example,
`"""[if] a missing profile is loaded [then] the CLI names the error, [else stop]."""`.
Legacy modules in `tests/requirement_intent_allowlist.txt` are temporary and
the list may only shrink.

- **No U+2014 or U+2013 characters** in any prose we author (code
  comments, docs, commit messages, issue text). Use a period, a comma, a
  colon, or parentheses. The audit trail in `.planning/milestones/`
  tracks this convention across phases.
- **Doppler for every secret. Never a `.env` file.** Spotify, cloud
  replicate, Picovoice, Groq, and anything else that needs a credential
  goes through `doppler run -- ...`. See
  [`docs/operator-setup.md`](docs/operator-setup.md) for the project and
  config names. New code that reads from `os.environ` without a Doppler
  wrapper in the Makefile will be sent back.
- **Fixture-first testing.** Anything that reads a real library
  (Rekordbox master.db, djay MediaLibrary.db, Serato crates, Traktor NML)
  gets a tiny deterministic fixture committed under `tests/fixtures/`.
  Tests that reach into the operator's actual library are not acceptable.
  `make fixture` rebuilds the Rekordbox fixture.
- **Plain English, short sentences.** This applies to docs, commit
  messages, and code comments. If a sentence needs two clauses, split
  it.
- **Apache-2.0, no relicensing.** By contributing you agree your
  contribution is Apache-2.0. Do not add GPL-or-later code to the
  in-repo tree; depend on it at runtime via pip if you need it and note
  it in [`NOTICE`](NOTICE) (see the mutagen entry as a worked example).

## Developer Certificate of Origin

All contributions are signed off. The `-s` flag certifies that you wrote
the patch or otherwise have the right to pass it on as an open-source
patch (the full DCO text is at https://developercertificate.org).

```bash
git commit -s -m "feat(<scope>): <summary>"
```

## Pre-release checks (`make release-check`)

Before cutting a release (tagging `vX.Y.Z` or opening a PR targeting a
`release/*` branch), run the aggregated gate:

```
source .venv/bin/activate
make release-check
```

This runs, in order:

1. `make test` -- full pytest suite.
2. `make lint` -- `ruff check apps tests scripts`.
3. `make build-dist` -- `python -m build` producing wheel + sdist under `dist/`.
4. `make reqs-check` -- verifies `reqs.json` is in sync with
   `.planning/REQUIREMENTS.md` (`python -m scripts.build_reqs_json --check`).
5. A best-effort `gh release view v1.0.1` sanity check (non-fatal; skipped
   if `gh` is unauthenticated or the release is not visible).

CI enforces the same target via
[`.github/workflows/release-check.yml`](.github/workflows/release-check.yml),
which runs on pushes to `release/*` branches, PRs targeting `release/*`,
and on `v*` tag creation.

## Licensing

By contributing to this project, you agree that your contributions will
be licensed under its [Apache License 2.0](LICENSE).

## Related documents

- [`README.md`](README.md) for the product-level overview.
- [`docs/branching.md`](docs/branching.md) for the post-v1 branching
  and PR convention.
- [`SECURITY.md`](SECURITY.md) for how to report vulnerabilities.
- [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) for community conduct.
- [`docs/operator-setup.md`](docs/operator-setup.md) for feature-by-feature
  system dependencies and Doppler configuration.
- [`docs/prior-art-and-communities.md`](docs/prior-art-and-communities.md)
  for the open-source prior art we read, cite, and cross-check against.
- [`.planning/MAINTAINER-REVIEW-QUEUE.md`](.planning/MAINTAINER-REVIEW-QUEUE.md) for
  the pending review items that gate v1.
- [`.planning/orchestration/gsd-orchestrator-playbook.md`](.planning/orchestration/gsd-orchestrator-playbook.md)
  for the phase-lifecycle orchestration model.
