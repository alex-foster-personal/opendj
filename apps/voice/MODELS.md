# apps/voice -- model + binary provenance

Phase 14 ships **code**, not weights. This file is the source-of-truth
for which wake-word + whisper models the voice daemon expects, where
they live, and how to fetch them. The binaries themselves are
gitignored (see repo-root `.gitignore`).

## openWakeWord

Default phrase: `"hey booth"` (voice-feasibility.md 1.4 rejects short
phrases; "hey deejay" collides with Siri).

- **Where**: `apps/voice/models/hey_booth.onnx` (override with
  `$VOICE_WAKE_MODEL_PATH`).
- **How to train**: 1-hour Colab per voice-feasibility.md 1.2.
  Alternatively, ship smoke with whatever prebuilt models
  openwakeword bundles (`alexa_v0.1.onnx`, etc.).
- **Licence**: Apache-2.0 (both library and any model we train).

## Porcupine (opt-in)

- `$PICOVOICE_ACCESS_KEY` must be set via `doppler secrets set
  PICOVOICE_ACCESS_KEY` (conventions: never `.env`).
- Custom keyword: `$VOICE_WAKE_KEYWORD_PATH=/path/to/hey_booth.ppn`.
- **Licence**: proprietary. Kept behind the env gate so the default
  build stays permissive (C9).

## whisper.cpp

Two acquisition paths; either works.

### Path A -- Homebrew (Sonoma+)

```bash
brew install whisper-cpp
which whisper-server          # should print a Homebrew prefix path
```

The Homebrew bottle may not ship with CoreML support; check with
`whisper-server --help | grep -i coreml`. If missing, go to Path B.

### Path B -- Build from source with CoreML

```bash
git clone https://github.com/ggerganov/whisper.cpp vendor/whisper.cpp
cd vendor/whisper.cpp
WHISPER_COREML=1 make -j
```

### Model weights

```bash
cd <repo-or-whisper.cpp>
bash ./models/download-ggml-model.sh small.en
bash ./models/generate-coreml-model.sh small.en
```

Expected size for `ggml-small.en.bin`: ~500 MB. The CoreML encoder
lives at `models/ggml-small.en-encoder.mlmodelc/` (directory).

## Checksums (fill in after local download)

| File                              | Size (MB) | SHA-256                         |
|-----------------------------------|-----------|---------------------------------|
| `ggml-small.en.bin`               | ~466      | _fill me after download_        |
| `hey_booth.onnx`                  | ~1-4      | _fill me after training_        |

_Checksum list is a human-maintained ledger. Plan 3 adds
`scripts/voice/audit-licenses.sh` but we leave weight-hashing manual
so we do not commit a binary blob to CI._

## Weights are NOT in git

The repo-root `.gitignore` includes `apps/voice/models/*.bin` and
`apps/voice/models/*.mlmodelc/` so accidental adds of 500 MB blobs
do not happen. `apps/voice/models/.gitkeep` is the only tracked file.

## Plan 1 (this phase) footprint

Plan 1 does NOT require any of these files to exist -- the pipeline
modules load without the wheels (lazy imports) and the CLI
`probe`/`bench`/`say` work on a fresh venv. Tests do not need any
weights. The full `run` daemon needs the weights; see README.
