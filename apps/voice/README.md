# apps/voice -- mid-set voice commands (VOICE-01)

Phase 14 of the _sync-tools project. Local-only wake-word + Whisper +
grammar-only intent pipeline so the DJ can drive the booth hands-free.

See `.planning/phases/14-voice-commands/` for CONTEXT + RESEARCH +
PLAN docs. See `docs/voice-feasibility.md` for the underlying stack
survey (latency, benchmarks, risks).

## One-command quickstart

```bash
# Probe the grammar + dispatch without a mic:
python -m apps.voice probe --text "find daft punk" --dry-bus

# Benchmark the grammar + dispatch loop:
python -m apps.voice bench --iterations 50 --text "find techno"

# Speak something via macOS `say`:
python -m apps.voice say "hello booth"

# List audio devices (needs PortAudio + sounddevice wheel):
python -m apps.voice list-devices

# Validate every real daemon dependency, including mic permission and the
# shared text-command endpoint, without claiming hardware success early:
python -m apps.voice.mic_daemon check

# Full agent-readable JSONL daemon (see below):
python -m apps.voice.mic_daemon run
```

## Intent cheat-sheet

| Say                                                   | Intent          | Notes                                     |
|-------------------------------------------------------|-----------------|-------------------------------------------|
| "find &lt;anything&gt;"                               | `SEARCH`        | universal library search                  |
| "what's this BPM" / "tempo"                           | `READ_BPM`      | reads current deck state                  |
| "what's the key" / "key"                              | `READ_KEY`      | Camelot first, else musical key           |
| "next track" / "play the next one"                    | `ADVANCE_QUEUE` | publishes; Phase 13 resolves              |
| "mute voice commands"                                 | `MUTE_VOICE`    | 30-minute default                         |
| "unmute voice"                                        | `UNMUTE_VOICE`  | clears mute                               |
| "save that last transition as cue points"             | `SAVE_CUE`      | blocked by the shared probe endpoint       |
| "rate this 5 stars"                                   | `RATE_TRACK`    | blocked by the shared probe endpoint       |

There is a printable one-pager at `apps/voice/CHEATSHEET.md` (Plan 3).

## Environment variables

All optional, doppler-sourced where noted.

| Variable                    | Default             | Purpose                                   |
|-----------------------------|---------------------|-------------------------------------------|
| `WAKE_BACKEND`              | `openwakeword`      | `openwakeword` / `porcupine` / `stub`     |
| `VOICE_WAKE_SENSITIVITY`    | `0.5`               | threshold in `[0.0, 1.0]`                 |
| `VOICE_WAKE_MODEL_PATH`     | -                   | override openWakeWord model file          |
| `STT_BACKEND`               | `whisper_cpp`       | `whisper_cpp` / `groq`                    |
| `VOICE_WHISPER_URL`         | `http://127.0.0.1:2022/inference` | whisper-server endpoint        |
| `VOICE_WHISPER_TIMEOUT_S`   | `15.0`              | HTTP timeout                              |
| `TTS_BACKEND`               | `say`               | `say` / `kokoro` / `piper` / `recording`  |
| `VOICE_SAY_VOICE`           | `the maintainer`              | macOS `say` voice                         |
| `VAD_BACKEND`               | `webrtc`            | `webrtc` / `amplitude`                    |
| `VOICE_VAD_MODE`            | `2`                 | webrtcvad aggressiveness 0-3              |
| `VOICE_VAD_TAIL_MS`         | `300`               | silence tail to cut utterances            |
| `VOICE_INPUT_DEVICE`        | -                   | `sd.default.device[0]` by default         |
| `VOICE_PROBE_URL`           | `http://127.0.0.1:9415/api/v1/voice/probe` | typed-command parity endpoint |
| `VOICE_DEBOUNCE_S`          | `2.0`               | ignore repeats within window              |
| `VOICE_MUTE_DURATION_S`     | `1800`              | 30 minutes                                |
| `VOICE_ENABLE_DESTRUCTIVE`  | `0`                 | duplicates `--enable-destructive` CLI flag|
| `PICOVOICE_ACCESS_KEY`      | -                   | **doppler**; only needed for Porcupine    |
| `GROQ_API_KEY`              | -                   | **doppler**; only needed for Groq STT     |

## Architecture

```
[Mic] -> sounddevice (audio.py)
      -> wake (openwakeword) (wake.py)
      -> vad silence-tail (vad.py)
      -> whisper.cpp HTTP (stt.py)
      -> POST /api/v1/voice/probe
      -> the same validated intent result as typed commands
      -> JSONL lifecycle + result state for agents
```

The mic daemon deliberately uses the existing voice probe HTTP route rather
than copying grammar or dispatch rules. Typed and spoken transcripts therefore
share intent, slot, destructive-blocking, and reply semantics.

## Safety rails

This app NEVER writes to `master.db` or the djay `database2` directly.
All destructive actions (`SAVE_CUE`, `RATE_TRACK`) publish events on
the shared-state bus; downstream writers (Phases 2-4) apply the
Phase 1 safety-rail pattern (backup, pgrep guard, dry-run, post-write
verify, reversal script) when they drain the queue.

The real mic daemon routes every transcript through `/api/v1/voice/probe`,
which blocks destructive intents. The older direct action-bus path retains its
separate `--enable-destructive` contract, but is not used by issue #204.
Destructive commands are unavailable through the issue #204 mic daemon.

## Install + runtime

### Pipe-only workflow (no mic, no whisper)

Works today on a fresh venv. Covers unit tests, `probe`, `bench`,
`say`. This is the CI path.

### Full daemon

Optional wheels + binaries:

1. `uv sync --extra voice` plus `brew install portaudio` on macOS.
2. whisper.cpp:
   - `brew install whisper-cpp` (Sonoma+) **or**
   - build from source with `WHISPER_COREML=1 make` under
     `vendor/whisper.cpp/`.
3. Download the small.en weights + CoreML encoder:
   - `bash ./models/download-ggml-model.sh small.en`
   - `bash ./models/generate-coreml-model.sh small.en`
4. Start the whisper daemon: `scripts/voice/start-whisper.sh`.
5. Start the web API on backend port 9415.
6. Run `python -m apps.voice.mic_daemon check`; it must emit `ready` and
   `stopped`, or it exits 2 with a terminal JSON error.
7. Run `python -m apps.voice.mic_daemon run`. It emits `starting`, `ready`,
   `wake_detected`, `capturing`, `transcript`, `submitted`, and `stopped`
   lifecycle records as JSONL. Missing dependencies and runtime audio loss
   emit `error` and exit 2.

See `apps/voice/MODELS.md` for checksums.

## Mic + mic-permission notes

- macOS will prompt on the first run that asks for mic access. Grant
  it to the terminal (or the packaged app once Phase 18 lands).
- Input device selection: we log the resolved device on startup. We
  warn if it looks like a built-in laptop mic, because that is the
  #1 reliability risk per `docs/voice-feasibility.md` 10.1. A
  close-talk headset (Shure WH20, AKG C520) is strongly recommended
  for live use.

## Troubleshooting

| Symptom                                      | Likely cause / fix                                             |
|----------------------------------------------|----------------------------------------------------------------|
| `whisper server unreachable`                 | Daemon not running. Start via `scripts/voice/start-whisper.sh`. |
| Mic warning but no wake-word fires           | Built-in mic picking up ambient noise; plug in headset.        |
| Intent never fires                           | Grammar is strict. Say exactly one of the phrases in the table. |
| Destructive intent reports `blocked: true`            | Expected: mic and typed probes share the safe endpoint. |
| Slow responses                               | Whisper cold start. Keep the daemon warm. `base.en` is faster. |

## Known limitations (Phase 14)

- **No LLM fallback.** Grammar is deterministic. Misses go to
  `data/voice/unmatched.log`; if >20% miss-rate in real sets, we ship
  Phase 14.2 with Phi-3-mini (voice-feasibility.md 4.3).
- **A real wake model is mandatory.** Set `VOICE_WAKE_MODEL_PATH` for
  openWakeWord or `VOICE_WAKE_KEYWORD_PATH` for Porcupine. The daemon refuses
  stock or stub fallback models because they would not prove the configured
  wake phrase.
- **Phase 5 state layer partial.** As of Phase 14 ship, Phase 5 only
  ships `schema` / `db` / `ids` / `paths`. The `events` writer is
  stubbed; the voice action bus falls back to JSONL until `events`
  lands.
- **Phase 12 transition schema not finalised.** `SAVE_CUE` reads a
  `transition` event kind from the bus; if Phase 12 ships a
  different key, a compatibility shim will be needed.
- **Mute and debounce share durable state.** Typed and mic probe requests load
  `mute_until` and `last_dispatch_at` from the voice settings store. Mute,
  unmute, and successful dispatches write through before the request returns.

## Files

- `__init__.py`, `__main__.py`: package + CLI entrypoint.
- `audio.py`: mic capture + device selection.
- `wake.py`: openWakeWord / Porcupine / stub backends.
- `vad.py`: WebRTC VAD + amplitude-only fallback.
- `stt.py`: whisper.cpp HTTP client + Groq hook.
- `tts.py`: macOS `say` + Kokoro / Piper hooks + recording test-double.
- `timings.py`: per-stage latency + budget warnings.
- `grammar.py`: deterministic intent parser.
- `bus.py`: JSONL stub + state-backed + in-memory buses.
- `context.py`: runtime state (mute / debounce / destructive).
- `actions.py`: dispatch table + 8 handlers.
- `confirm.py` (Plan 3): verbal yes/no loop.
- `settings.py` (Plan 3): persistent mute + backend selection.
- `mic_daemon.py`: real mic lifecycle, local whisper health, shared probe
  submission, cancellation, and agent-readable JSONL.
