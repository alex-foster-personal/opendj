# apps/vocals -- agent notes

CLI: `python -m apps.vocals` (scan / trickle / one / **from-stems**).

## from-stems (stems -> vocal-cache, CPU, no demucs)

When `data/state/stems/<stable_id>/` already has a 4-stem bundle but
`data/state/vocal-cache/<stable_id>.json` is missing or stale, derive the
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
