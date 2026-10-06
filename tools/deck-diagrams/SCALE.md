# Scale status

Tracking: https://github.com/private_owner/music-dj-tools/issues/371

## AC (hover)

Every control in every non-blocked `devices/*/deck.html` must:
- render as `.control` with `data-fig`, `data-label`, `data-midi-name|type|ch|code`
- share the `pointerenter` handler that fills `#readout`

Gate script:

```sh
uv run python tools/deck-diagrams/scripts/verify_hover_ac.py
```

## Current (2026-07-24)

- 100+ devices with hover AC PASS
- Mix of official Pioneer MIDI-list diagrams + Mixxx tier-2 ingest
- Catalog: `catalog/controllers.json` (`stats.hover_ok`)

## Scale-to-100 gate (met)

1. FLX10 checksum + hover PASS
2. Skill documents letter / section / named / Mixxx paths
3. `verify_hover_ac.py` green across device tree (blocked stubs skipped)
4. Batch tools: `batch_build_all.py`, `ingest_mixxx.py`, `ingest_mixxx_all.py`, `generate_html.py`

## Next hardening

- Plate-tuned coords (not grid) for Mixxx devices
- Official PDF fetch for remaining AlphaTheta SKUs (URL casing / product folders)
- Pad-mode layers beyond SHIFT
- Deep-research popularity ranks
