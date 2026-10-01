# AGENTS.md

Guide for coding agents and human contributors working in this repository. For the
human-facing overview see `README.md`; for the contribution process see `CONTRIBUTING.md`.

## What this project is

music-dj-tools (openDJ) is a local-first DJ library toolchain and an agent-drivable DJ rig:

- A FastAPI daemon (`apps/webui/server`, loopback `127.0.0.1:8585`, no authentication) over a
  shared SQLite state layer (`data/state/state.db`).
- A SvelteKit frontend (`apps/webui/frontend`, dev server on `:5173`) whose main screen,
  `/performance`, is a four-deck Web Audio performance rig over your real library.
- Adapters and sync tooling for Rekordbox, djay Pro, Serato and Traktor (`apps/sync`,
  `apps/adapters`, `apps/shared`).
- `open-dj/`: a published cross-format metadata spec, schema and conformance corpus, with its
  reference implementation in `apps/open_dj`.

Python packages live in a flat `apps/` tree, so anything can `import apps.shared.*`. Start at
`docs/architecture.md` and `docs/glossary.md`.

## Setup (one path)

Prerequisites: macOS, git, [uv](https://docs.astral.sh/uv/getting-started/installation/), and
Node.js 22.14 or newer.

```bash
uv sync --frozen --extra dev --python 3.11.15
corepack enable
cd apps/webui/frontend
pnpm install --frozen-lockfile
```

- Python is pinned by `.python-version` (3.11.15); the floor is 3.11.
- `uv.lock` is the lockfile. Change dependencies only with `uv add`, `uv remove` or `uv lock`,
  in the same commit as the `pyproject.toml` edit. Never use `pip`.
- The frontend uses **pnpm only**. Do not create a `package-lock.json` there.
- Run Python through `uv run <cmd>` (it uses the repository's `.venv`). If `uv run` appears to
  use another project's environment, run it from the repository root.
- Heavy ML packages (torch, demucs) never enter the repository environment. They run from
  standalone PEP 723 scripts under `uv run` (see `scripts/stem_bundle_worker.py`).

## Running tests

Run the narrowest scope that covers your change. Do not run the whole suite by default.

```bash
uv run pytest tests/<area> -n 4              # backend, one area
uv run pytest tests/<area>/test_x.py -q      # one file

cd apps/webui/frontend
pnpm test:unit                               # frontend unit tests (node:test)
pnpm check                                   # svelte-check: must report 0 errors
node --experimental-strip-types --test tests/unit/<file>.test.mjs   # one frontend file
```

Notes on test environments:

- **External fixtures.** The Rekordbox and USB-export fixtures live on an external fixture host
  that most contributors do not have. A test that needs one fails loudly when the host is
  unavailable, so a missing fixture never reads as green. If you knowingly lack the host, set
  `MDT_ALLOW_MISSING_FIXTURES=1` (exactly `1`) and those tests skip instead. It does not cover
  a fixture that is present but stale or corrupt; that still fails. `MUX_FIXTURE_HOST` points
  the fixture resolver at a different host.
- **Optional packages.** Tests for optional features (`scipy`, `librosa`, `soundfile`, `modal`,
  and `sentry-sdk` from the `observability` extra) skip cleanly when the package is missing.
  When you add such a test, guard it with `pytest.importorskip("<package>")`.
- **Parallel runs.** `pytest -n <N>` (xdist) needs every worker to collect the same tests in
  the same order. Never parametrize from an unsorted `set` or `dict` of strings; wrap it in
  `sorted(...)`. Tests must not create or delete files inside the repository tree (use
  `tmp_path`), because other tests read the tree concurrently.
- A failing test is evidence. Fix the code or the test for a stated reason; do not delete,
  weaken or skip a test to get green.

Lint with the pinned ruff: `make lint LINT_PATHS="path/to/file.py"`. It reports an absolute
count that is not zero today; keep your changed files clean.

## Conventions

- **Conventional commits**: `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`, `ci:`,
  with an optional scope, for example `fix(sync): ...`. One logical change per commit. Sign
  off with `git commit -s` (Developer Certificate of Origin, see `CONTRIBUTING.md`).
- **American English spelling** in code, identifiers, docs, comments and commit messages
  (color, analyze, behavior, license).
- **Punctuation**: do not use the Unicode characters U+2013 or U+2014 anywhere. Use `-`, `--`,
  a comma or a colon.
- **Nothing is mocked and nothing is invented.** Do not fabricate data to make a screen or a
  test look right. A UI control with no real data source renders inert with the tooltip
  `not implemented`, and a missing waveform or beat renders as an explicit absence, never a
  made-up value. Tests use real fixtures; they do not stub the system under test.
- **Fail fast.** No hidden defaults and no fallbacks that mask a failure. Raise an explicit
  error that names what was missing. Do not swallow exceptions.
- **Verify the presence of the good thing**, not the absence of the bad one. A check that
  finds no failures may simply be broken; show that a check can fail before trusting that it
  passed.
- **Six-rail safety for vendor writes.** Any code that writes to a Rekordbox, djay, Serato or
  Traktor library, or an operator's audio files, follows the pattern in `apps/sync/safety.py`:
  typed confirmation, running-app check, timestamped backup, dry-run default (a live write
  needs both `--live` and `--i-understand-the-risks`), atomic write, and post-write readback
  with a reversal script. Details in `CONTRIBUTING.md`.
- **Secrets** come from the environment, never from committed files. Never commit a `.env`
  file, key or token.
- **Type hints** on Python code; keep changes small and the names self-explanatory.
- **Numeric readouts** in the UI (counts, Hz, ms, ratios) carry a hover `title` that explains
  what the number is.
- **Tests that carry a requirement id** (`@pytest.mark.requirement("<ID>")`) state one
  single-line intent in the module or test docstring: `[if] X [then] Y, [else stop]`.

## Pull request flow

1. Branch from `main` (from a fork if you lack write access): `feat/<slug>`, `fix/<slug>`,
   `docs/<slug>`, `chore/<slug>`, `refactor/<slug>` or `test/<slug>`. Never push to `main`.
2. Make the change with tests. Run the scoped backend tests, and for frontend changes
   `pnpm check` plus the relevant unit tests.
3. Open a pull request with a plain-language description: what changed, why, and how you
   verified it. Keep it to one logical change.
4. Resolve every review thread: fix it, or reply with the reasoning. Maintainers merge once
   required checks pass.
5. Security issues go through `SECURITY.md`, not a public issue.

## Where things are

```
apps/shared/        paths, DB wrappers, the state layer
apps/webui/         FastAPI daemon (server/) and SvelteKit frontend (frontend/)
apps/sync/          vendor sync and the six-rail safety helpers
apps/open_dj/       open-dj reference implementation
open-dj/            published spec, schema, conformance corpus
scripts/            standalone workers and tooling
tests/              pytest suite (fixtures under tests/fixtures/)
docs/               architecture, format notes, glossary, operator setup
```
