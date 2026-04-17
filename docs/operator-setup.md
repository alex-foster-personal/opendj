# Operator Setup - music-dj-tools

*The minimum you need to install on your Mac before any feature works. Each section is independent - install only what you want.*

## Baseline (every feature needs this)

- macOS 13+ (arm64 or x86_64).
- Python 3.11+. CI pins Python to 3.11 because `tflite-runtime` (pulled transitively by `openwakeword`) has no wheel for 3.12+ at the time of writing. Local dev on 3.14 also works for non-voice features (`docs/install-notes.md` documents the 3.14 dev path).
- Homebrew.
- Doppler CLI + access to the `construct` / `dev_af` project (only needed for commands that touch secrets; see Spotify and Cloud sections).
- Create a venv and install Python deps:

```bash
cd /Users/dev3/Music/music-dj-tools
python3 -m venv .venv
source .venv/bin/activate
pip install --no-build-isolation -r requirements.txt
python -m pyrekordbox download-key   # one-time: cache master.db decryption key
```

The `--no-build-isolation` flag is required because `madmom` needs `cython` and `numpy` at setup time but declares no build-requires (see the comment in `requirements.txt`).

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
- `numpy<2` pin in `requirements.txt` is required for `madmom` compatibility. Do not upgrade numpy separately in this venv.
- No further host setup; librosa + madmom + scipy are pure Python wheels.

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
- Entry point: `python -m apps.open_dj ...` (see `apps/open_dj/cli.py`).

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

## Troubleshooting

- "Rekordbox is running" error on any write path: quit Rekordbox (Cmd+Q) before running commands that write to `master.db`. The safety rail uses `pgrep` to abort early.
- "djay is running" error on any playlist or cue write: quit djay Pro AI first. Same reason.
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
