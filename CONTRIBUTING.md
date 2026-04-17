# Contributing to music-dj-tools

Thanks for taking the time to contribute. This doc is the short list of
rules that keep the project coherent across many sub-agent and human
contributors. Read it once; the conventions are load-bearing.

## Table of contents

1. [Project structure](#project-structure)
2. [Dev setup](#dev-setup)
3. [Running tests and lint](#running-tests-and-lint)
4. [Conventional commits](#conventional-commits)
5. [The six-rail safety pattern](#the-six-rail-safety-pattern)
6. [Proposing a new phase](#proposing-a-new-phase)
7. [House rules](#house-rules)
8. [Developer Certificate of Origin](#developer-certificate-of-origin)
9. [Licensing](#licensing)

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

`--no-build-isolation` is required because `madmom` needs `cython` and
`numpy` at setup time but declares no build-requires.

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
   you touch the file.
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
   merged.

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

- **No U+2014 characters and no U+2013 characters** in any prose we author (code
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

## Licensing

By contributing to this project, you agree that your contributions will
be licensed under its [Apache License 2.0](LICENSE).

## Related documents

- [`README.md`](README.md) for the product-level overview.
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
