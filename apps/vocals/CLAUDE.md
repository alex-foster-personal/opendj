# apps/vocals -- agent notes

CLI: `python -m apps.vocals` (scan / trickle / one / **from-stems**).

## scan buckets

Reports `pvdi`, `cached_demucs`, `missing_file`, `missing_analysis`,
`broken_vocal_cache`, **`stem_derivable`** (loadable stem bundle, no valid
vocal-cache yet), and **`todo`** (true demucs gap). The `todo` ETA uses only
`todo` track lengths at the SPIKE-B2 1.36x CPU rate.

## trickle

Default demucs queue is **`todo` only**. Tracks with a loadable stem bundle are
**`stem_derivable`**: use `from-stems`, not demucs. Pass **`--demucs-even-if-stems`**
to include them in the demucs queue (legacy behaviour).

**`--max-minutes N`**: wall-clock budget for the run. Skips starting tracks whose
estimate exceeds remaining budget and continues with shorter tracks later in the
queue. **`--limit`** counts started tracks only.

Overnight replacement for the retired `scripts/fill_vocal_gap.sh`:

```sh
MDT_VOCAL_WORKER_DEVICE=cpu python -m apps.vocals trickle --live --max-minutes N --data-dir <data>
```

## from-stems (stems -> vocal-cache, CPU, no demucs)

When `(private path)/<stable_id>/` already has a 4-stem bundle but
`(private path)/<stable_id>.json` is missing or stale, derive the
blue-bar regions from the stems (RMS ratio + hysteresis). No GPU.

```sh
python -m apps.vocals from-stems --dry-run
python -m apps.vocals from-stems --live
python -m apps.vocals from-stems --live --stable-id <id>
```

`--dry-run` or `--live` is **required** (no silent default). Mutating
`trickle` uses the same rule.

Cache contract: `apps/vocals/cache.py`. Listing UI reads vocals via
`rb_vendor.vocals_for_content` on hydrated rows (PreviewStrip blue bars).
