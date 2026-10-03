# Stem API probe

Minimal CLIs to A/B **LALAL.AI**, **AudioShake**, and **Moises** against local
htdemucs bundles. Opinionated only where needed: prefer 4-stem
`vocals/drums/bass/other` WAVs for mute/solo parity.

## Setup

1. Put keys in repo `.env` (never commit):

```
LALAL_API_KEY=...
AUDIOSHAKE_API_KEY=...
MOISES_API_KEY=...          # optional / partner
MOISES_API_BASE=...         # required if using Moises partner API
AUDIOSHAKE_API_BASE=https://groovy.audioshake.ai
```

2. Signup / credits (opened in Chrome during setup):

- https://www.lalal.ai/api/
- https://www.lalal.ai/pricing/
- https://www.audioshake.ai/
- https://developer.audioshake.ai/
- https://moises.ai/
- https://moises.ai/pricing

## Run

```sh
uv run tools/stem-api-probe/probe.py --provider lalal \
  --input "<repo>/Music/Warm Up Set/Artist Name - Track Title (Original Mix).mp3" \
  --out .tmp/.tmp_stem_probe/lalal

uv run tools/stem-api-probe/probe.py --provider audioshake \
  --input "...same..." --out .tmp/.tmp_stem_probe/audioshake
```

Probe is fail-loud: endpoint shapes differ by plan; first successful keyed
responses should be pasted back so we harden download -> WAV landing.

## What "under 1 dB" means (SDR)

**SDR** (Source-to-Distortion Ratio) is a log ratio of target energy vs error
energy, usually via `museval` on MUSDB-style reference stems. A **~1 dB** gap
between two systems is often near the edge of average audibility on the
benchmark mix. It is **not**:

- crispness / edge definition
- metallic / phasy artifact rate
- transient smear on kicks
- vocal ghosting into INST
- **proportion of hard splits that just work** for DJ mute (dense EDM, chops)

For DJ mute/solo, listen for bleed + artifacts on a hard OLTF subset, and treat
mean SDR as a secondary CI gate only when you have reference stems (MUSDB18-HQ
/ MoisesDB), not as the primary product score.
