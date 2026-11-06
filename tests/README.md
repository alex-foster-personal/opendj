# Tests

This directory holds the Python test suite (pytest). Tests for other languages
live next to the code they test:

| Suite | Where | Run |
|:--|:--|:--|
| Python (this directory) | `tests/` | `just test-scope tests/<area>` |
| Web UI unit tests | `apps/webui/frontend/tests/unit/` | `cd apps/webui/frontend && pnpm run test:unit` |
| Web UI end-to-end (Playwright) | `apps/webui/frontend/tests/e2e/` | `cd apps/webui/frontend && pnpm run test:e2e` |
| Audio engine (Rust) | `apps/audio-engine/tests/` | `cargo nextest run --manifest-path apps/audio-engine/Cargo.toml` |
| Desktop shell (Rust) | `apps/desktop/src-tauri/` | `cargo nextest run --manifest-path apps/desktop/src-tauri/Cargo.toml` |
| Launcher (TypeScript, Rust) | `apps/launcher/` | `cd apps/launcher && pnpm test` and `pnpm run test:rust` |

## Quick start

```bash
uv sync --locked --extra dev   # pytest is in the dev extra; --locked never rewrites uv.lock
just test-scope tests/<area>   # one file or folder, single process, no collect floor
just test                      # the whole suite: xdist, plus a collect floor of 3700 tests
just test-serial               # the whole suite in one process, for readable tracebacks
```

`just test` passes `--collect-floor`, so a run that collects fewer tests than the
floor fails instead of passing on a partial suite. A scoped run has no floor,
because it is meant to collect less.

Without `--extra dev`, `uv run pytest` can fall back to a `pytest` on your PATH
running a different Python. The root `conftest.py` fails loud when that happens;
`just interpreter-check` is the standalone probe.

## Folder map

Most folders mirror an `apps/` package of the same name. The larger ones:

| Folder | What it covers |
|:--|:--|
| `scripts/` | The repo tooling in `scripts/`: CI planners, gates, release and audit scripts. The largest folder. |
| `webui/` | The FastAPI daemon (`apps/webui`): routes, state, port config. |
| `shared/` | `apps/shared`: state layer, provenance, hashing, play orders. |
| `cloudsync/` | CloudSync policy, digests and the property simulation. |
| `quality/`, `perf/` | Quality ratchets and performance budgets. |
| `engine_core/` | The packaged engine's Python core (`apps/engine_core`). |
| `analysis*/`, `beatbench/` | Analysis pipelines (beatgrid, key, loudness, structure, waveform) and their benches. |
| `lyrics*/`, `vocals/`, `stems/` | Lyrics alignment and search, vocals cache, stem bundles. |
| `sync/`, `adapters/`, `reconcile/`, `open_dj/` | Vendor sync, Serato and Traktor adapters, reconciliation, the open-dj format. |
| `agentic_testing/` | The persona-driven agentic test harness. |
| `fixtures/` | Shared fixtures and the fixture resolver (see below). |
| `support/` | Pytest plugins, including `spawned_server`. |
| `data/` | Small committed snapshots read by tests. |

Top-level `tests/test_*.py` modules predate the folders; new tests go in a folder.

## Fixtures

`tests/fixtures/_resolver.py` resolves a fixture by name, in this order:

1. a committed directory `tests/fixtures/<name>/`;
2. a symlink `tests/fixtures/<name>` to a directory elsewhere (handy for a local setup);
3. a committed `tests/fixtures/<name>.extern` marker, whose one line is a subpath under
   the external fixture host. The host defaults to an external drive; set
   `MUX_FIXTURE_HOST` to point somewhere else.

The large Rekordbox fixtures (`rekordbox`, `rb-usb-export`, `rb-usb-export-big`,
`rb-usb-export-onetera-20260805`) are `.extern`-backed: they hold real library data and
are not published.

- A missing fixture **fails** the test by default, so a fresh clone or CI host cannot read
  green while skipping every ingest and write-safety test. When the default external fixture
  host is unmounted and `MUX_FIXTURE_HOST` is unset, `resolve_required_fixture` skips with
  `UNAVAILABLE` instead; an explicit `MUX_FIXTURE_HOST` that is missing, a missing subpath
  on a present host, and a contract mismatch still fail. On a machine that knowingly lacks the
  host but uses an explicit override path, set `MDT_ALLOW_MISSING_FIXTURES=1` to skip instead.
- The Rekordbox fixture DB is checked against `tests/fixtures/rekordbox.manifest.json`.
  A checksum mismatch **always fails**, even with `MDT_ALLOW_MISSING_FIXTURES=1`, because
  that is a data-integrity error rather than a missing fixture.
- Never commit a regenerated Rekordbox DB. `python -m scripts.make_rb_fixture --force`
  rebuilds it from a live library; move the result onto the fixture host.
  `.gitignore` blocks `tests/fixtures/rekordbox/master.plain.db`.

## Markers that need hardware, credentials or an opt-in

All markers are registered in `pyproject.toml` (`--strict-markers` rejects unknown ones).
These do not run on a plain checkout:

| Marker | Runs when |
|:--|:--|
| `live_db` | `--live-db` is passed (touches a live Rekordbox or djay DB) |
| `live_r2` | `R2_*` credentials are in the environment |
| `live_github` | `MDT_LIVE_GITHUB=1`; a `gh` auth failure is then a hard failure |
| `live_openrouter` | `OPENROUTER_API_KEY` is set (spends real credit) |
| `real_library` | a real `state.db` is present |
| `audio_hw` | real audio hardware is attached (skipped on CI) |
| `requires_darwin` | macOS |
| `requires_audio_stack`, `requires_madmom`, `requires_joblib` | the optional Python packages are installed |
| `requires_fpcalc`, `requires_ffmpeg` | the binary is on `PATH` |
| `requires_canonical_decode` | an `odj-audio` build with decode is available |

## Rules for new tests

- Trace each test to a requirement with `@pytest.mark.requirement("AREA-NN")`, using an id
  from `.planning/REQUIREMENTS.md`.
- A new test folder must be claimed by a scope (or an `always` entry) in
  `ci/test-scopes.yml`; `(private path)` fails on an unclaimed test module,
  so a new folder cannot silently go unrun. The gate scope is separate: a new root-level
  module or folder must also be in `pytest_scope` in the `justfile` or named in
  `KNOWN_UNSCOPED` in `(private path)`.
- Shared test helpers live in a module beside the tests (for example
  `tests/<area>/<area>_support.py`), never in `conftest.py`. Any conftest
  change is a `GLOBAL_PATTERN` that selects the full suite
  (`scripts/ci_test_selection_rules.py`, `(private path)`).
- Start servers through the `spawned_server` fixture (`tests/support/spawned_servers.py`).
  It kills the whole process group on teardown, on failure and when pytest itself dies.
- Never import torch or demucs. Heavy ML dependencies stay out of the repo environment and
  run as standalone PEP 723 scripts through `uv run`.
- Never mock the system under test into passing; a test that cannot measure fails or
  reports why.

## CI

- The pull request fast lane splits the suite into 5 shards (`pytest-split`, least-duration
  balancing).
- On a pull request, `scripts/ci_test_selection.py` can run only the test modules the change
  can affect, found through the import graph (`scripts/affected_tests.py`). It is gated by
  the `CI_PR_TEST_SELECTION` repository variable, and anything it cannot resolve runs the
  full suite. The merge queue runs the full suite on the exact tree that lands.
- `ci/test-scopes.yml` drives the job-level planner (`scripts/ci_plan.py`), which decides
  which CI seams a change needs.
- Pytest exit code 5 (no tests collected) counts as a **failure**. The one exception is an
  affected-only shard whose JUnit report shows every selected module skipped at module level.
