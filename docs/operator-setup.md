# Operator Setup - music-dj-tools

_The minimum you need to install on your Mac before any feature works. Each section is independent - install only what you want._

## Environment variables

The authoritative list of environment variables consumed by music-dj-tools
lives in [`.env.sample`](../.env.sample) at the repo root. Each entry in that
file has a comment explaining which subsystem reads it. Copy it to `.env` for
local dev (gitignored) or - preferred for anything sensitive - store the
values in Doppler under `construct` / `dev_af` and invoke commands via
`doppler run --`.

## Logs and client-error triage

All runtime logs stay in the engine data directory, so they survive app restarts and can be collected before deciding whether an incident needs Sentry triage.

| Path | Writer | Retention and purpose |
| --- | --- | --- |
| `<data-dir>/logs/engine.log` | Bundled engine stdout and stderr | Appended across launches. Rotates at 5 MiB; keeps at most 20 timestamped archives per stream (~100 MiB); archives older than 7 days are deleted. Rotation stops when free space under the data-dir mount falls below 1 GiB (one ERROR is logged). |
| `<data-dir>/logs/engine-warn.log` | Python logging and uvicorn errors | JSONL warnings and errors only, with a `boot_id` on every record. Same 5 MiB live cap, 20-archive count cap, 7-day retention, and 1 GiB free-space rotation halt as `engine.log`. `uvicorn.access` lines are muted with one WARNING when the rate exceeds 50 lines/s for 60 s. Use `tail -f` for the focused incident stream. |
| `<data-dir>/logs/webui-client-errors-YYYY-MM-DD.log` | Browser error API | Full daily JSONL browser-error records. The file is never rewritten by triage. |
| `<data-dir>/logs/webui-performance-YYYY-MM-DD.log` | Live page telemetry | Boot-deferred client performance samples (10 s while visible). Error-bearing rows also appear in `GET /api/v1/errors`. |
| `<data-dir>/logs/webui-client-errors-YYYY-MM-DD.triage.jsonl` | Browser error triage API | Append-only decisions: `fix`, `no-fix`, or `duplicate`, with a required reference. |
| `~/.local/share/music-dj-tools/webui/webui-backend-YYYY-MM-DD.log` | `just webui-backend` | Development backend stdout and stderr. |

For the Chrome loop, `scripts/run_chrome_loop.sh --engine` appends the direct engine output to `<chrome-loop-data-dir>/logs/engine.log`; do not start the engine bare if the output needs to survive the terminal session.

List outstanding browser incidents with `GET /api/v1/client-errors?untriaged=1`. Record a decision with `PATCH /api/v1/client-errors/<event_id>` and a JSON body such as `{"disposition":"no-fix","ref":"issue #896"}`.

Aggregate ERROR/WARN records from every server-readable sink for the last hour (or a custom window) with `GET /api/v1/errors?since=<ISO8601>`. Each event carries `source`, `level`, `message`, and `stack` / `url` / `context` when the sink has them. Missing sinks are listed under `sinks` without failing the request.

## Baseline (every feature needs this)

- macOS 13+ (arm64 or x86_64).
- Python 3.11+. CI pins Python to 3.11 because `tflite-runtime` (pulled transitively by `openwakeword`) has no wheel for 3.12+ at the time of writing. Local dev on 3.14 also works for non-voice features (`docs/install-notes.md` documents the 3.14 dev path).
- Homebrew.
- Doppler CLI + access to the `construct` / `dev_af` project (only needed for commands that touch secrets; see Spotify and Cloud sections).
- Install uv, then create the environment from the lockfile (see the Install section of the
  repository `README.md` for the full path, including the frontend):

```bash
uv sync --frozen --extra dev --python 3.11.15
```

Rekordbox's `master.db` key comes from `pyrekordbox` and is cached under `~/.pyrekordbox/` on
first decrypt; there is no separate key-download step.

## Feature: Reconcile + djay matching + shared state (Milestones 1-5)

- Nothing extra. Works on the vanilla baseline.
- Entry points that exist today:
  - `python -m apps.shared.state ...` (shared-state CLI).
  - `python -m apps.audit.rekordbox_vs_music` (find broken Rekordbox to file links).

## Feature: Dedup + Chromaprint (Phase 7)

- `brew install chromaprint` (provides the `fpcalc` binary).
- Verify: `scripts/check-chromaprint.sh` (prints the installed version on success, prints a remediation message and exits non-zero on failure).
- Used by `pyacoustid` via subprocess. If `fpcalc` is missing, `apps.shared.fingerprints.compute()` raises `ChromaprintMissing` with the remediation message.

## Feature: Analysis pipeline (Phase 6)

- `brew install ffmpeg` (also used by Phase 12 set recording).
- The default backend is `librosa`: pure PyPI wheels (librosa ISC, scipy BSD-3-Clause) via the `analysis` extra, so analysis works on a clean machine with no git builds. It does beats/BPM, onsets, key, RMS, and energy -- no downbeat tracking.
- `librosa+madmom` is the optional dev-only learned beat/downbeat backend (`--backend librosa+madmom`). madmom is a git-HEAD install from `requirements.txt`; its code is BSD-3-Clause but its bundled pre-trained models are CC-BY-NC-SA 4.0 (non-commercial only), so it must never be an automatic fallback or part of a distributed install.
- `numpy<2` pin in `requirements.txt` is required for `madmom` compatibility. Do not upgrade numpy separately in this venv.

## Feature: Spotify import (Phase 9)

- Spotify developer app credentials (free at https://developer.spotify.com).
- Store in Doppler, never in a `.env` file:

```bash
doppler secrets set SPOTIFY_CLIENT_ID=... SPOTIFY_CLIENT_SECRET=... \
  --project construct --config dev_af
```

- Run under Doppler so the env is injected:

```bash
doppler run -- python -m apps.spotify ...
```

- Cache directory: `data/spotify/cache/` (24 h TTL; gitignored).

## Feature: USB sync (Phase 10)

- A Pioneer-compatible USB drive, formatted exFAT or FAT32.
- `brew install ffmpeg` if you will transcode AIFF to MP3 during export.
- The kaitai runtime (`kaitaistruct>=0.11`) is in `requirements.txt` for the vendored `rekordbox_pdb` parser; no separate install.

## Feature: Cloud sync + web UI (Phase 11)

- Litestream: `brew install benbjohnson/litestream/litestream`.
- An S3-compatible object store (Cloudflare R2, AWS S3, or similar). Credentials via Doppler.
- Syncthing (optional, for replicating the audio files themselves between Macs): `brew install syncthing`.
- Python deps (FastAPI, uvicorn, httpx) are already in `requirements.txt`.
- `boto3` is lazy-imported by `apps.cloud.replicate` so tests do not require it; install it separately (`pip install boto3`) when you actually run cloud replication.

## Feature: Set recording (Phase 12)

Phase 12 captures a full DJ set to disk as a rolling MP3 plus a structured timeline of deck events.

- Virtual audio driver (required): `brew install --cask blackhole-2ch`. BlackHole 2ch is MIT-licensed and is the macOS community default. Loopback (Rogue Amoeba, paid) also works if you already have it; any virtual audio device that exposes itself to AVFoundation is fine.
- Route djay Pro's main output through BlackHole (or a multi-output device that sends to BlackHole plus your real speakers).
- `brew install ffmpeg` (the recorder shells out to ffmpeg in AVFoundation mode; Python never imports an audio library).
- No PortAudio / no sounddevice for this feature.
- First run:

```bash
python -m apps.sets start
# ... play a set ...
python -m apps.sets stop
```

- Other subcommands: `resume`, `status`, `list`, `prune`, `classify`, `label`, `train`, `replay`.
- If the recorder sees 30+ seconds of pure silence it warns you that BlackHole is probably not wired into djay's output.
- Storage budget: a 4 h set at 320 kbps MP3 is roughly 575 MB. Use `--retention-days` on `prune` to drop old segments; the timeline is tiny and stays.

## Feature: Voice commands (Phase 14)

Phase 14 is local-only: wake word + whisper.cpp + deterministic grammar. See `apps/voice/README.md` and `apps/voice/MODELS.md` for detail.

Host-install steps:

1. Wake-word model. The repo expects `apps/voice/models/hey_booth.onnx` (override with `$VOICE_WAKE_MODEL_PATH`).
   - Quickest smoke: use a prebuilt openWakeWord model that ships with the wheel (e.g. `alexa_v0.1.onnx`). Good for plumbing validation; not suitable for live DJ use because the wake phrase will not be "hey booth".
   - Real use: train a "hey booth" model via the 1-hour Colab notebook referenced in `.planning/phases/14-voice-commands/14-RESEARCH.md` / `docs/voice-feasibility.md` section 1.2, then drop the `.onnx` into `apps/voice/models/`.
   - Licence: Apache-2.0 (openWakeWord library and any model you train).
2. whisper.cpp. Either path works:
   - Path A (Homebrew, Sonoma+): `brew install whisper-cpp`. Confirm the CoreML encoder with `whisper-server --help | grep -i coreml`; if absent, use Path B.
   - Path B (build from source with CoreML):

```bash
git clone https://github.com/ggerganov/whisper.cpp vendor/whisper.cpp
cd vendor/whisper.cpp
WHISPER_COREML=1 make -j
```

3. Whisper model weights (`small.en` is the default target):

```bash
cd <repo-or-whisper.cpp>
bash ./models/download-ggml-model.sh small.en
bash ./models/generate-coreml-model.sh small.en
```

Expected size for `ggml-small.en.bin` is ~466 MB. These weights are NOT in git (see `.gitignore`).

4. Start the whisper HTTP server (default endpoint `http://127.0.0.1:2022/inference`):

```bash
scripts/voice/start-whisper.sh
```

5. PortAudio: already bundled with the `sounddevice` wheel on macOS. No extra install.
6. Microphone permission: macOS System Settings > Privacy & Security > Microphone > enable access for Terminal (or iTerm, or whichever terminal you use).
7. Smoke the daemon:

```bash
python -m apps.voice list-devices           # confirm input device selection
python -m apps.voice probe --text "find techno" --dry-bus   # no mic needed
python -m apps.voice run                    # full daemon, read-only intents
python -m apps.voice run --enable-destructive   # live mode with SAVE_CUE + RATE_TRACK
```

Hardware note: a close-talk headset (Shure WH20, AKG C520, or similar) is strongly recommended for live use. The built-in MacBook mic is the #1 reliability risk per `docs/voice-feasibility.md` section 10.1.

Secrets (only if you swap the default backends):

- `PICOVOICE_ACCESS_KEY` (Porcupine wake-word, proprietary): `doppler secrets set PICOVOICE_ACCESS_KEY=... --project construct --config dev_af`.
- `GROQ_API_KEY` (Groq STT fallback): same pattern via Doppler.

Do not put either key in a `.env` file.

## Feature: Open-dj adapters (Phase 15 / 16)

- Nothing extra. Pure Python. `jsonschema[format]` and `rfc8785` come from `requirements.txt`.
- Entry point: `python -m apps.open_dj.cli ...` (see `apps/open_dj/cli.py`).

## Feature: Launcher (Phase 17 / 18)

The Tauri-based menu-bar palette lives at `apps/launcher/`.

- Xcode Command Line Tools (for clang + codesign). Usually already installed on a dev Mac: `xcode-select --install`.
- Rust stable toolchain: `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`.
- Node 20+ and pnpm: `brew install node pnpm`.
- Build + run in dev:

```bash
cd apps/launcher
pnpm install
python scripts/bootstrap_db.py   # first-run: build the search index
pnpm tauri dev
```

- Hotkey: `Alt+Space` (fallback `Ctrl+Cmd+Space`). Type 2+ characters to filter. Drag a row onto djay Pro's Deck A / B to load.

## Building the dmg

Prerequisites for `just dmg` (first successful end-to-end build in PR #510, Fri 28-Sat 29 Aug 2026). The recipe stages the engine payload, bundles the Tauri shell, then mounts and verifies the artifact in one pass; any missing piece exits non-zero.

Run `just dmg-preflight` first. It checks every prerequisite below in one read-only pass (about 8 seconds) and names EVERY one that is unmet, rather than making you discover them one failed build at a time. `just dmg` runs it too, and so does `scripts/ship_dmg.sh --dry-run` whenever the plan needs a build (OPS-11). Build the SPA as `cd apps/webui/frontend && pnpm run build`: corepack resolves the `packageManager` pin from the current directory, so the `--dir` form run from the repo root silently uses whatever pnpm is on PATH instead of the pinned one.

- cargo >= 1.85. The crate graph pulls edition2024 crates; `rustup update stable`.
- tauri-cli ^2: `cargo install tauri-cli --locked --version '^2'`.
- A uv-resolved repo venv. Its interpreter version drives the staged runtime (3.11 and 3.14 are proven; the runtime prune list has been glob-based since #510, so it follows whatever minor is staged).
- SPA pre-built: `pnpm build` in `apps/webui/frontend`. The payload builder fails loudly on a missing or stale build; it never rebuilds the SPA behind your back.
- Clean working tree. Release builds are cut from clean trees only; any dirty file aborts.
- Lane label: plain `just dmg` builds the real product (Open DJ). A lane build requires double intent: `MDT_LANE_LABEL=X` in the env AND `just dmg X` together (OPS-08; a lone env var or a lone recipe arg is refused).
- arm64 only (v1 decision). The artifact does not run on Intel Macs.
- Signing is required, not optional. `MDT_MACOS_SIGNING_IDENTITY` (a Developer ID Application identity) and `MDT_MACOS_NOTARY_KEYCHAIN_PROFILE` (an `xcrun notarytool store-credentials` profile) must both be set, or the build is refused before it starts. The one deliberate way past that is `MDT_SHIP_UNSIGNED=1`, which builds an unsigned dev image and says so loudly; a downloaded unsigned image arrives quarantined, and `ship_dmg.sh` clears the attribute on install for that case only. Setting `MDT_SHIP_UNSIGNED=1` together with an identity is refused as contradictory. This is the Developer ID direct-download path; `scripts/ship_appstore.sh` is a separate path with a different certificate off the same Team ID.

## Safety rail updates in the v1.0 ship window

Three rail-2 and rail-6 tightenings landed just before the v1.0 ship
sweep. They change how you see failures but do not change the CLI
contract for normal use.

- **`pgrep` is now fail-closed (PR #81).** If `pgrep` is missing or
  errors on your machine, `apps/sync/playlist_apply.py` refuses to run
  a live write instead of silently skipping the running-app check. The
  escape hatch is `--force-no-pgrep`, which you should only pass after
  manually confirming Rekordbox AND djay Pro are both quit. See
  `apps/sync/playlist_apply.py:79` and `:492`.
- **`allow_app_running=True` is test-only (PR #86).** `apps/tags/apply.py`
  now raises `RuntimeError` if the `allow_app_running` kwarg is passed
  from outside a pytest run (`_in_pytest()` guard at
  `apps/tags/apply.py:129`). Production callers cannot silently skip
  the process check.
- **Cloud write guard is fail-closed (PR #85).** The lock probe in
  `apps/webui/server/deps.py:get_lock_status` used to swallow probe
  exceptions and return `None`, which the write guard read as "no peer
  holds the lock". It now returns a sentinel that
  `get_write_state` translates into `503 lock_probe_failed`. Paired
  fix in `apps/cloud/replicate.py` keeps the cloud lock held until the
  Litestream subprocess has actually exited (`_wait_for_proc_exit`
  with a 30-second timeout plus a hard-kill fallback).
- **Playlist-apply verify is in-transaction (PR #81).** Readback
  verification runs inside the per-op `BEGIN IMMEDIATE` / `COMMIT`
  block. A mismatch triggers `ROLLBACK` and the op is marked `failed`
  with `readback mismatch (rolled back): ...`; the bad rows are never
  durable on disk.

If a write fails with one of the new messages, treat the underlying
cause as the bug. Do not reach for `--force-no-pgrep` to get past a
real "app still running" condition.

## Troubleshooting

- "Rekordbox is running" error on any write path: quit Rekordbox (Cmd+Q) before running commands that write to `master.db`. The safety rail uses `pgrep` to abort early (PR #81 tightened this to fail closed when `pgrep` itself is missing).
- "djay is running" error on any playlist or cue write: quit djay Pro AI first. Same reason.
- `PlaylistApplyError: pgrep not found (or failed)`: your host has no `pgrep` on `$PATH`. Install one (`brew install pgrep` ships with the `proctools` formula, or `pgrep` is in BSD userland on macOS by default). Only after you have confirmed Rekordbox and djay are both quit, rerun with `--force-no-pgrep` to override rail 2.
- `503 lock_probe_failed` from the web UI: a cloud peer lock probe raised. Check `apps/cloud/replicate.py` logs for the underlying exception and confirm your Litestream / R2 credentials are still valid before retrying writes.
- `readback mismatch (rolled back): ...` in a playlist apply run: the write was refused and rolled back; no durable change. Investigate the specific mismatch in the error message before retrying.
- `tflite-runtime` wheel not found during `pip install`: your Python is probably 3.12 or newer. Install Python 3.11 in a separate venv (via `pyenv install 3.11` or a conda env) and reinstall. This only matters for the voice feature; everything else is fine on 3.14.
- `fpcalc: command not found`: `brew install chromaprint`.
- `ffmpeg: command not found`: `brew install ffmpeg`.
- Voice: `whisper server unreachable`: start the whisper daemon with `scripts/voice/start-whisper.sh` and check it is listening on `127.0.0.1:2022`.
- Voice: wake word never fires: you are probably on the built-in laptop mic. Plug in a headset. The daemon logs a warning on startup when it suspects a built-in mic.
- Voice: recognised text never fires an intent: the grammar is strict. Say one of the phrases in `apps/voice/CHEATSHEET.md` exactly.
- Set recording: the MP3 segments are silent: djay Pro is not routing audio through BlackHole. In djay Pro AI, set the main output to BlackHole 2ch (or a multi-output device that includes it).
- `madmom` install fails with `ModuleNotFoundError: No module named 'Cython'`: rerun `pip install --no-build-isolation -r requirements.txt`.

## Conventions reminder

- Doppler for every secret. Never `.env`. This includes Spotify, cloud-sync credentials, Picovoice, and Groq.
- Do not commit model weights, audio files, or database snapshots. See `.gitignore`.
- macOS only for v1. Linux-native paths are noted in `docs/install-notes.md` for the pieces where they differ.

## Credits

See `docs/prior-art-and-communities.md` for the open-source prior-art this project builds on (pyrekordbox, rekordcrate, openWakeWord, whisper.cpp, BlackHole, and others).
