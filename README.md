# music-dj-tools

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Release stage: alpha](https://img.shields.io/badge/release-alpha-d97757.svg)](#release-stage-alpha)
![Status: performance rig in build](https://img.shields.io/badge/status-performance%20rig%20in%20build-orange.svg)
[![Substrate: v1.0-rc3, Apr 2026](https://img.shields.io/badge/substrate-v1.0--rc3%20(Apr%202026)-blue.svg)](RELEASE-NOTES-v1.0-rc3.md)

## Release stage: alpha

Open DJ is alpha software. Features, screens and stored data formats can change
between releases, and some controls are not finished yet. Keep a backup of your
DJ library before letting Open DJ write to it.

**An agent-drivable DJ rig, built to open the DJ-software moats.**

The incumbent tools (rekordbox, Serato, Traktor) are closed at every layer that matters:
closed library formats, hardware locked to vendor software, no automation surface, and no way
to extend them. This project attacks that.

The current build target is [`/performance`](apps/webui/frontend/src/routes/performance): a
pixel-faithful, local-only clone of the rekordbox 7 Performance-mode screen (4-deck layout),
running on a Web Audio engine over your real library.

**Parity is the strategy, not the goal.** Cloning a familiar shell means a working DJ picks it
up with no retraining, and it gives every component an unambiguous acceptance test: does it
match rekordbox on real data. The product is what parity unlocks afterwards.

| Goal | What it means here |
|:---------------------------|:-----------------------------------------------------------|
| **Agent-drivable** | Every UI control is also reachable as a typed command, so agents can operate, test and extend the rig. No vendor tool permits this. |
| **Missing capabilities** | State-of-the-art stem separation, real library management, inline lyrics. Things rekordbox has no equivalent for. |
| **Low bloat, high perf** | Fast and small, not a plugin host. |
| **Community modding** | Easy to fork and extend, which follows from open formats plus an automation surface. |
| **No lock-in** | Local-first. The library outlives any one vendor. |
| **Cross-platform library** | The library is the asset; it should move between tools freely. |
| **Honest library view** | Shows when a track's audio file is unavailable so missing links can be investigated. |

## The rule of the build

> **Nothing is mocked, nothing is invented.** A control with no real data source renders
> visually authentic but inert, with the tooltip `not implemented - see PARITY-TODO`.

Its consequences appear at every layer, and they are not separate policies: show OUR track
counts and never the vendor screenshot's; draw no beat that does not come from a real PQTZ
row; render a missing waveform as an explicit dash; reject a stems manifest that does not
declare its `layout`; put every destructive vendor write behind six rails. If a change would
make the UI show something the data does not support, it is wrong regardless of how good it
looks.

## Start here

| Document | What it is for |
|:-------------------------------------------|:--------------------------------------|
| [`docs/architecture.md`](docs/architecture.md) | The shape of the system, and why. |
| [`docs/glossary.md`](docs/glossary.md) | Terms a newcomer cannot google (PQTZ, PVDI, stable_id, warm/wired/cold). |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Setup, tests, conventions and the pull request flow. |

Deeper dives: [`docs/architecture/audio-timing.md`](docs/architecture/audio-timing.md) (the
transport clock, Beat Sync phase-lock, stem alignment) and
[`docs/architecture/open-dj.md`](docs/architecture/open-dj.md) (the published format).

Each checkout claims its own backend and frontend ports. Use `just webui-ports` to see
this checkout’s endpoints and `just webui-ports-check` before starting the servers.

## Two products, one substrate

The repo contains two products built roughly four months apart. Reading it as one system is
the main way to get confused.

**April 2026 (v1.0), the library-truth layer.** A bi-directional sync, enrichment and
authoring toolchain: ingest from Rekordbox / djay Pro / Serato / Traktor into a shared state
DB, analyse, dedup, reconcile broken links, smart playlists, set recording, Spotify import,
voice, a Tauri launcher. CLI-first, GSD-managed, genuinely shipped (see
[`CHANGELOG.md`](CHANGELOG.md) and the RELEASE-NOTES files).

**July 2026 onward, the performance rig.** `/performance`, the Web Audio engine, stem
separation, vocal analysis, MIDI controllers. This is the current build.

The second is not a rewrite of the first. **The April work is substrate**, deliberately
harvested by the new build: measured Sat 15 Aug 2026, the webui imports 13 April-era modules,
`apps/shared` alone 31 times. That is a live dependency, not a vestige.

The useful distinction is not "old versus new" but how far each module has travelled toward
the rig:

| Tier | Meaning | Modules |
|:---------|:-------------------------------------------|:------------------------------|
| **Warm** | Serving real data to `/performance` today | `shared`, `webui`, `sync`, `analysis`, `tags`, `stems`, `vocals`, `reconcile`, `adapters`, `open_dj` |
| **Wired** | Endpoint exists and the UI calls it, but no data has been created yet | `smartlists`, `pairings`, `spotify`, `voice` |
| **Cold** | Built, tested, not yet surfaced in the rig | `sets`, `play_analytics`, `dedup`, `dj_copilot`, `cloud`, `audit`, `launcher` |

**Cold does not mean dead.** Each cold module is staged work with a named entry point on the
parity board. Deleting one destroys deliberate substrate.

MIDI and controllers are **in scope, secondary priority** (decided Sat 15 Aug 2026). WebMIDI
core, a mapping contract, action glue and device maps for DDJ-FLX10, Reloop Mixtour and
DDJ-400 are merged on trunk, and the rig has been played live on a DDJ-400. On-screen parity
comes first; controller work re-lands after the core surface is solid.

## Install

One verified path. You need macOS, [git](https://git-scm.com/), and
[uv](https://docs.astral.sh/uv/getting-started/installation/). The committed `.python-version`
selects Python 3.11.15; `pyproject.toml` also enforces Python 3.11 or newer, and `uv` fetches
the interpreter for you.

```bash
git clone https://github.com/alex-foster-personal/opendj.git
cd opendj

# Python environment (.venv), reproducible from uv.lock
uv sync --locked --extra dev --python 3.11.15
uv run --no-sync python --version

# Frontend (Node.js 22.14 or newer; pnpm only, no package-lock.json)
corepack enable
cd apps/webui/frontend
pnpm install --frozen-lockfile
```

`uv.lock` and `.python-version` are the reproducible development-environment contract.
Commands use the repository's `.venv` through `uv run`; activate it only when an interactive
shell needs bare `python` or `pytest`. `corepack` ships with Node.js 22; if your Node.js does
not include it, install the pnpm version named by `packageManager` in
`apps/webui/frontend/package.json` by any other means.

### Running the tests

```bash
uv run pytest tests/<area> -n 4     # scoped run; start with the area you changed
cd apps/webui/frontend && pnpm test:unit
```

Some tests need data this repository does not carry: the Rekordbox and USB-export fixtures
live on an external fixture host. When that host is unavailable, those tests **fail loudly by
default** instead of silently passing. If you do not have access to it, set
`MDT_ALLOW_MISSING_FIXTURES=1` (exactly `1`) to turn that failure into a skip, for example:

```bash
MDT_ALLOW_MISSING_FIXTURES=1 uv run pytest tests/<area> -n 4
```

It only covers a fixture host that is not available. A fixture that is present but stale or
corrupt (a checksum or contract mismatch) still fails. `MUX_FIXTURE_HOST` points the resolver at
a different fixture host if you keep one elsewhere.

Tests for optional features skip cleanly when their package is not installed (`scipy`,
`librosa`, `soundfile`, `modal`, and `sentry-sdk`, which is the `observability` extra). Heavy
ML packages are deliberately not in the repo venv (see below).

### Rekordbox library data

Rekordbox's `master.db` is SQLCipher-encrypted. This project reads a **static decrypted
working copy** at `data/master.plain.db`. Two distinct files live under `data/`:

- `data/master.db.copy` is a byte-for-byte snapshot of the live `master.db`, so it is still
  encrypted and a plain `sqlite3` client opening it gets `file is not a database`.
  `apps.shared.paths.copy_live_dbs()` writes it, and `python -m apps.audit.rekordbox_vs_music`
  calls that on every run.
- `data/master.plain.db` is the decrypted copy everything else reads.

`python -m apps.shared.state.cli ingest-rb` produces the plain copy for you: when it is
absent, the command decrypts `data/master.db.copy` into it with `sqlcipher_export` before
ingesting, and prints the path it wrote. The CLI sequence below therefore works as written on
a fresh checkout, with the decrypt happening implicitly on the first `ingest-rb`. Details:

- Copy `master.db`, `master.db-wal` and `master.db-shm` **together**. Copying `master.db`
  alone silently misses recent writes and looks merely out of date rather than broken.
- The SQLCipher key comes from `pyrekordbox` itself and is cached under `~/.pyrekordbox/`
  after the first decrypt; there is no separate key-download step. See
  [`docs/install-notes.md`](docs/install-notes.md) and pyrekordbox's own docs. If the key is
  unavailable the decrypt fails loudly, naming the decrypt as the failure, and nothing is
  ingested.
- The plain copy is **static**: once it exists `ingest-rb` reuses it rather than
  re-decrypting, so refreshing is deliberate. Refreshing means `ingest-rb --refresh-decrypt`
  (or a manual re-decrypt) **and then** `rm -rf data/state/anlz-cache/`, because that cache
  embeds djmdCue-derived cues.

Optional external CLIs:

- `ffmpeg` on `$PATH` for USB sync transcode paths.
- `fpcalc` (chromaprint, `brew install chromaprint`) for dedup and the fingerprint matcher
  fallback. Verify with `scripts/check-chromaprint.sh`.
- `doppler` for secret injection on the Spotify importer and cloud replicate targets.

## Running the rig

Claim the worktree’s ports, then start the two processes in separate terminals from the
repository root:

```bash
just webui-ports-claim
just webui-ports

# Terminal 1: reloadable FastAPI backend
just webui-backend

# Terminal 2: Vite frontend, with the API proxy derived from the backend port
just webui-frontend
```

Open `/performance` on the frontend endpoint printed by `just webui-ports`.
`make webui.dev` starts only the backend. If the library is empty, check the backend’s
configured data directory and import status rather than connecting to another checkout’s server.

## The substrate layer: CLI entry points

Every entry point is `python -m apps.<package>`. Each command below was run against this tree
on Sat 15 Aug 2026 and its flags confirmed from the live `--help`.

```bash
# 1. Audit: reconcile Rekordbox references against what is actually on disk.
#    Takes no arguments; copies the live DBs into data/ and prints a report.
#    This is what populates the encrypted data/master.db.copy snapshot.
python -m apps.audit.rekordbox_vs_music

# 2. State: initialise the shared state DB, then run the Rekordbox ingest adapter.
#    ingest-rb decrypts data/master.db.copy -> data/master.plain.db on first run
#    and reuses that plain copy afterwards; --refresh-decrypt forces a rebuild.
#    It is a dry run unless you pass --write.
python -m apps.shared.state.cli init
python -m apps.shared.state.cli ingest-rb --write
python -m apps.shared.state.cli stats

# 3. Sync: dry-run the RB-canonical playlist diff into djay under the six rails.
#    --dry-run is the default; a live write needs --live AND --i-understand-the-risks.
python -m apps.sync.playlist_apply --dry-run

# 4. Vocals: report vocal-bar coverage, then backfill from existing stem bundles on CPU.
python -m apps.vocals scan
python -m apps.vocals from-stems --dry-run

# 5. Voice: parse a transcript through the grammar and dispatch, without a microphone.
python -m apps.voice probe --text "what's the bpm of this track"
```

More is wired through the Makefile: `make test`, `make cov`, `make integration`,
`make webui.dev`, `make webui.openapi`, `make cloud.replicate`, `make cloud.self-check`,
`make spotify-import URL=...`, `make spotify-rematch PLAYLIST_ID=...`, `make audit-cues`,
`make audit-sync`, and `make vocal-kpi-live-check`. The live vocal KPI gate has
matching Make and just entry points:

```bash
make vocal-kpi-live-check
just vocal-kpi-live-check
VOCAL_CACHE_DIR=/absolute/path/to/vocal-cache just vocal-kpi-live-check
```

`VOCAL_CACHE_DIR` defaults to `data/state/vocal-cache`. This is deliberately a
machine-local acceptance command, not part of any portable release gate: it
validates and prints the current real cache, while portable tests replay the
committed sanitized fixture in `tests/fixtures/vocal-kpi/`. It exits non-zero
when the directory is absent, a JSON record is malformed, no instrumented
record exists, or a measured duration cannot be resolved. Success prints the
canonical current-window derivation as JSON.

Heavy ML never enters the repo venv. `apps/stems` and `apps/vocals` shell out to standalone
PEP 723 scripts (`scripts/stem_bundle_worker.py`, `scripts/vocal_region_worker.py`) that
declare their own dependencies and run under `uv run`. Importing torch into the daemon's venv
is a regression even if it works locally.

## Safety: the six-rail pattern

Every destructive write to an operator's files goes through six rails, so no single mistake
can corrupt a DJ library. Reference implementation: `apps/sync/safety.py`.

1. **Typed confirmation.** Operator types a non-trivial string.
2. **Running-app check.** `pgrep` for Rekordbox / djay / Serato / Traktor. A missing or
   erroring `pgrep` is a hard error, not a skip. Override is `--force-no-pgrep`.
3. **Timestamped backup** of every file about to be modified. Rail 6 reads from it.
4. **Dry-run default.** A live write needs both `--live` and `--i-understand-the-risks`.
5. **Atomic write** (temp file + `os.rename`). Never truncate in place.
6. **Post-write readback verification**, plus an emitted reversal script.

Imported by `apps/sync/{playlist_apply,apply_analysis,apply_ratings,apply_cues}.py` and
`apps/smartlists/{writers,djay_writer}.py`; `apps/reconcile` and `apps/spotify` carry
equivalents. `apps/stems`, `apps/vocals` and `apps/sets` are exempt because they write only
derived artifacts under `data/`, never operator files. Do not add six-rail ceremony there,
and do not skip it for anything touching a vendor DB or an operator's audio.

## open-dj

[`open-dj/`](open-dj/) is not a module of this project. It is a **published standard with its
own licence, schema, conformance corpus and versioning policy**, which this project happens to
be the first implementation of. Given the thesis is opening the DJ-software moats, it is the
most strategically loaded artifact in the repo.

Four vendors, four incompatible storage models, and no provenance anywhere: a BPM is just a
number, with no record of who analysed it, when, or with what confidence, so a human-corrected
BPM is silently clobbered by the next app's auto-analysis. open-dj wraps analysed values so
disagreement between sources is representable rather than resolved by whoever wrote last, and
canonicalises via RFC 8785 JCS so two independent tools produce byte-identical output.

Spec `open-dj/spec/v0.2/`, schema `open-dj/schema/v0.2/`, reference implementation
`apps/open_dj/`, write-capable adapters `apps/adapters/{serato,traktor}`. Full treatment in
[`docs/architecture/open-dj.md`](docs/architecture/open-dj.md), including known gaps between
the published spec and what was actually built.

## Repo layout

A flat `apps/` package, so anything under `apps/` can `import apps.shared.*` without path
gymnastics.

```
apps/
  shared/          # paths, DB wrappers, the state layer (the spine)
    state/         # schema, stable_id, provenance envelope, event bus, StateWriter
  webui/           # FastAPI daemon (:8585) + SvelteKit frontend (:5173, /performance)
  sync/            # RB <-> djay playlist / cue / beatgrid / rating sync, six-rail safety
  analysis/        # librosa + madmom + MIK backends
  stems/           # stem-bundle orchestration (workers run out-of-venv)
  vocals/          # vocal-region derivation into data/state/vocal-cache/
  reconcile/       # broken-link repair for moved audio files
  tags/            # tag unification and ID3 / MP4 / Vorbis tag writing
  adapters/        # write-capable open-dj adapters (serato, traktor)
  open_dj/         # open-dj reference parser + validator + canonicaliser
  smartlists/      # rule engine + materialiser
  pairings/        # transition edges between tracks
  spotify/         # Spotify importer + rematch
  voice/           # wake-word + VAD + Whisper STT + intent bus
  sets/            # set recording + replay + transition classifier
  play_analytics/  # play-history analytics
  dedup/           # chromaprint dedup + cluster apply
  dj_copilot/      # PLAY IT solver + next-track suggester
  cloud/           # Litestream + R2 lock + Syncthing fallback
  audit/           # reconcile + sync-diff + cue comparison tooling
  launcher/        # Tauri cmd-K shell + drag-core adapters
  artist_counts/   # artist tallies
open-dj/           # the published spec, schema, conformance corpus
scripts/           # standalone PEP 723 workers and operational tooling
data/              # local, gitignored; working copies of DBs, caches, stem corpora
docs/              # architecture, format reverse-engineering, runbooks, glossary
tests/             # pytest suite
```

## Working in this repo

- [`CONTRIBUTING.md`](CONTRIBUTING.md) is the contribution guide; [`AGENTS.md`](AGENTS.md)
  carries the same conventions for coding agents.
- Never mock data: a control with no real data source renders inert (see the rule above).
- Use `-` or `--` for pauses and compound words; do not use the Unicode characters U+2013 or
  U+2014 in any file.

## Thanks

music-dj-tools builds on the reverse-engineering and open-source work of several projects and
communities, including pyrekordbox and the Pioneer database research community.

## License

Apache License 2.0. See [`LICENSE`](LICENSE) for the full text and [`NOTICE`](NOTICE) for
attribution of third-party runtime dependencies. Audio tags are read with `tinytag` (MIT).
Third-party components retain their own licenses; the source dependency and lock files
identify the configured packages. The open-dj spec
itself is published under CC BY 4.0; see [`open-dj/LICENSE-SPEC.md`](open-dj/LICENSE-SPEC.md).

## Contact

- Issues + discussion: https://github.com/alex-foster-personal/opendj/issues
- Security reports: [`SECURITY.md`](SECURITY.md)
- Code of conduct: [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)
