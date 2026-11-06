# Contributing to Open DJ

Thanks for taking the time to contribute. This doc is the short list of
rules that keep the project coherent across many human and agent
contributors. Read it once; the conventions are load-bearing.

## Reporting problems and contributing on GitHub

The public repository, [alex-foster-personal/opendj](https://github.com/alex-foster-personal/opendj),
is a published copy of the source. Day-to-day development happens in a private repository, and
releases are published to the public one.

- **Bugs and feature requests:** open an issue on the public repository using one of the forms.
  Each public issue is copied into the development tracker, and its status is posted back on the
  public issue.
- **Questions and ideas:** use Discussions on the public repository.
- **Security problems:** report them privately through the public repository's Security tab
  (see [`SECURITY.md`](SECURITY.md)). Never in a public issue.
- **Pull requests:** welcome on the public repository. Because it is a published copy, a pull
  request is not merged there directly: a maintainer applies it in the development repository
  with you credited as co-author, and the change reaches the public repository with the next
  publish. The pull request is then closed with a link to where it landed.

## Table of contents

1. [Project structure](#project-structure)
2. [Dev setup](#dev-setup)
3. [Running tests and lint](#running-tests-and-lint)
4. [Branching and pull requests](#branching-and-pull-requests)
5. [Conventional commits](#conventional-commits)
6. [The safety rails](#the-safety-rails)
7. [Proposing a larger change](#proposing-a-larger-change)
8. [House rules](#house-rules)
9. [Developer Certificate of Origin](#developer-certificate-of-origin)
10. [Licensing](#licensing)

## Project structure

The repo is a flat `apps/` monorepo. High-level map (see
[`docs/developer-guide.md`](docs/developer-guide.md) for the exhaustive version):

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
tests/           pytest suites; fixtures under tests/fixtures/
scripts/         one-off tools, build helpers, fixture builders
```

## Dev setup

One verified path. You need macOS, [git](https://git-scm.com/) and
[uv](https://docs.astral.sh/uv/getting-started/installation/). `uv` fetches Python 3.11.15
(the version in `.python-version`) for you. See
[`docs/operator-setup.md`](docs/operator-setup.md) for feature-specific system dependencies
(ffmpeg, chromaprint and so on).

```bash
git clone https://github.com/alex-foster-personal/opendj.git
cd opendj

uv sync --frozen --extra dev --python 3.11.15

corepack enable
cd apps/webui/frontend
pnpm install --frozen-lockfile
```

`uv.lock` is the reproducible Python environment. Use `uv run <command>` to run inside
`.venv`. Never use `pip`: dependencies change through `uv add`, `uv remove` and `uv lock`,
with the lockfile in the same commit. The frontend needs Node.js 22.14 or newer and **pnpm
only** (there is no `package-lock.json` for the frontend). `corepack` ships with Node.js 22; if
yours does not include it, install the pnpm version named by `packageManager` in
`apps/webui/frontend/package.json` some other way.

Rekordbox's key for `master.db` comes from `pyrekordbox` and is cached under `~/.pyrekordbox/`
on first decrypt. There is no separate key-download step; see the rekordbox library data section of
[`docs/developer-guide.md`](docs/developer-guide.md).

## Running tests and lint

Run only the tests for the area you changed:

```bash
make test                  # portable core pytest slice (same --ignore set as ci.yml)
make test-optional-native  # madmom/vocals/USB-agent/sets suites excluded from core
make test-full             # full tree with floor 3700 (dispatch full-ci locally)
make cov                   # adds coverage (term + HTML report in htmlcov/)
make integration           # pytest -m integration (slower, disk-touching)
make reqs-check            # verify requirements.txt matches reqs.json lock
make ci                    # what CI runs: reqs-check + cov
make fixture               # rebuild the Rekordbox test fixture DB
make rb-parity-check       # fast Rekordbox iteration gate (unchanged by #159)

uv run pytest tests/<area> -n 4          # backend, scoped
cd apps/webui/frontend
pnpm test:unit                           # frontend unit tests
pnpm check                               # svelte-check (types)
```

On macOS, run `make test-optional-native` before release to exercise Quartz/cliclick
Pioneer agent tests. Linux CI covers these via `full-ci.yml` / periodic-checks.

The full suite is large; CI runs it, so you rarely need to.

### Fixtures you may not have

The Rekordbox and USB-export fixtures live on an external fixture host that most contributors
do not have. With the default host path unmounted and `MUX_FIXTURE_HOST` unset,
`resolve_required_fixture` **skips** with `UNAVAILABLE` so generic CI lanes do not fail on a
missing drive. If you set `MUX_FIXTURE_HOST` to a path that does not exist, or the host is
mounted but the fixture subpath is missing, tests still **fail loudly** so a missing fixture
never reads as a green run on a host that promised that tree. If you knowingly lack the host
while using a non-default override, set `MDT_ALLOW_MISSING_FIXTURES=1` (exactly `1`; any other
value keeps the failure) and those tests skip instead:

```bash
MDT_ALLOW_MISSING_FIXTURES=1 uv run pytest tests/<area> -n 4
```

The variable only covers an unavailable host. A fixture that is present but stale or corrupt
(a checksum or contract mismatch) still fails. `MUX_FIXTURE_HOST` points the fixture
resolver at a different host.

### Optional packages

Tests for optional features skip cleanly when their package is missing: `scipy`, `librosa`,
`soundfile`, `modal`, and `sentry-sdk` (the `observability` extra, `uv sync --extra
observability`). Do not add heavy ML packages (torch, demucs) to the repository environment;
they run from standalone PEP 723 scripts under `uv run`.

### Lint

```bash
make lint                                # pinned ruff over apps, tests, scripts
make lint LINT_PATHS="path/to/file.py"   # only the files you touched
```

`make lint` reports the absolute count, which is not zero today; the CI gate only fails when a
count grows. Whole-file formatting of files you did not otherwise change is forbidden except in
an issue #4456 part pull request that formats a measured cold set (see `(private path)`).
After issue #4456 finishes, it is forbidden again. Local blame with ignore-revs applied:
`just blame <file>` (passes `--ignore-revs-file` as a flag; do not set
`git config blame.ignoreRevsFile`, because git config is shared across worktrees). Format-only
commits on main merge with merge commits only, never squash or rebase. Otherwise match the style
of the surrounding code; a formatting diff mixed into a real edit hides the edit and conflicts
with every open pull request on that file.

## Branching and pull requests

No change lands on `main` directly. Every change, including a one-line docs tweak, goes
through a pull request from a branch (from a fork if you do not have write access):

```
feat/<slug>     # new feature
fix/<slug>      # non-trivial bug fix
docs/<slug>     # docs only
chore/<slug>    # tooling, CI, dependency bumps
refactor/<slug> # behaviour-preserving code move
test/<slug>     # tests only
```

Before opening the pull request: run the scoped tests for what you touched, keep the diff
to one logical change, and write the description in plain language (what changed, why, and
how you checked it). A maintainer then applies it in the development repository once its
checks pass and review threads are resolved, and closes your public pull request with a link
to where it landed (see
[Reporting problems and contributing on GitHub](#reporting-problems-and-contributing-on-github)).

## Conventional commits

Every commit message follows
[Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short summary>

[optional body]

[optional footer, including Signed-off-by]
```

Common types: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `build`,
`ci`. Scopes are usually an app module (`fix(sync):`) or an area such
as `docs(governance):`.

## The safety rails

Every destructive write path in this repo goes through a safety rail set. If you add or modify a
code path that writes to Rekordbox, djay, Serato, Traktor, or any operator file on disk, the rails
apply. Vendor library DB writes use the seven-rail harness in
[`apps/sync/safety.py`](apps/sync/safety.py) (`LiveWriteSession`, plus a backstop rail 0 for
one-way import mode); the full list, the writer conventions around it and its known gap are in
[the developer guide](docs/developer-guide.md#safety-the-seven-rail-harness), the single source.
Older docs and module docstrings call this the six-rail pattern.

What a new writer must do, in short:

- Enter `LiveWriteSession` for any live vendor DB write: it takes the backup, checks the target app
  is quit, requires `--i-understand-the-risks` and starts the reversal script.
- Default to a dry run whose output is the diff the live run applies; write only with `--live`.
- Fail closed when the running-app check cannot run (missing or erroring `pgrep`), with
  `--force-no-pgrep` on `playlist_apply` as the only operator override (PR #81).
- Never truncate in place: read back inside the same `BEGIN IMMEDIATE` ... `COMMIT` as the write
  for DB writers (`apps/sync/playlist_apply.py`, PR #81), or write a temp file and `os.replace` it.
- Ship a reversal script; a new writer without one will not be merged.

Parallel fail-closed rule for cloud write guards: if a peer-lock probe
raises, the FastAPI dependency must return `503 lock_probe_failed`
rather than defaulting to "no peer holds the lock"
(`apps/webui/server/deps.py:get_lock_status`, PR #85). The lock itself
must be held until the Litestream subprocess has actually exited
(`apps/cloud/replicate.py:_wait_for_proc_exit`).

Voice intents that are destructive are additionally gated behind
`--enable-destructive`. New destructive CLI surfaces should follow the
same pattern.

## Proposing a larger change

For a non-trivial feature, open an issue first with: one paragraph of scope, the safety rails
it touches, the test fixtures it needs, and a rough exit criterion. For smaller changes, just
open a pull request.

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
  colon, or parentheses.
- **Use American English spelling** (color, analyze, behavior, license).
- **The app never shows invented data.** A control with no real data source
  renders inert rather than showing invented data, and code fails loudly
  instead of falling back silently. Tests and Storybook may use doubles for a
  dependency, never for the thing under test; see "Test doubles and locked
  real fixtures" in `AGENTS.md`.
- **Secrets come from the environment, never from committed files.** Spotify,
  cloud replicate, Picovoice, Groq, and anything else that needs a credential
  is read from environment variables injected at run time (the Makefile
  targets use `doppler run -- ...`; any secrets manager that exports the same
  variables works). Never commit a `.env` file or a key. See
  [`docs/operator-setup.md`](docs/operator-setup.md) for the variable names.
- **Fixture-first testing.** Anything that reads a real library
  (Rekordbox master.db, djay MediaLibrary.db, Serato crates, Traktor NML)
  gets a tiny deterministic fixture under `tests/fixtures/`.
  Tests that reach into the operator's actual library are not acceptable.
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

1. `make test` -- portable core pytest slice (no madmom/Quartz/cliclick required).
2. `make quality` -- full enforced quality ratchet (`make lint` reports debt only).
3. `make build-dist` -- `python -m build` producing wheel + sdist under `dist/`.
4. `make waveform-native-verify` -- wheel consumer smoke for the native extension.
5. `make reqs-check` -- verifies `reqs.json` is in sync with
   `.planning/REQUIREMENTS.md` (`python -m scripts.build_reqs_json --check`).
6. A best-effort `gh release view v1.0.1` sanity check (non-fatal; skipped
   if `gh` is unauthenticated or the release is not visible).

Optional native suites are not part of `release-check`; run
`make test-optional-native` locally or dispatch `full-ci.yml` for the complete tree.

CI enforces the same target via
[`.github/workflows/release-check.yml`](.github/workflows/release-check.yml),
which runs on pushes to `release/*` branches, PRs targeting `release/*`,
and on `v*` tag creation.

## Licensing

By contributing to this project, you agree that your contributions will
be licensed under its [Apache License 2.0](LICENSE).

## Related documents

- [`README.md`](README.md) for the product-level overview.
- [`SECURITY.md`](SECURITY.md) for how to report vulnerabilities.
- [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) for community conduct.
- [`docs/operator-setup.md`](docs/operator-setup.md) for feature-by-feature
  system dependencies and credential configuration.
