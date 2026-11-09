# Pin ledger export corpus

Locked feedback store snapshot used to regenerate `user-prompts-pins.jsonl` (in a
provenance store, published to `odj-private/threads/`) without a live engine lane. Sources:

- `comments.json` and `archive-*.json` from silver `com.opendj.desktop` feedback
  (rsync `~/Library/Application Support/com.opendj.desktop/feedback/`).
- `scripts.feedback_prompts_export` reconciles duplicate ids across live and
  archive copies (newest stamp wins; live wins ties). The corpus no longer
  needs manual deduplication; historical drops were only for the old refusal.
- Issue #4088 packet pins missing from that store are appended to `comments.json`.

Refresh:

```sh
uv run --no-sync python -m scripts.feedback_prompts_export \
  --feedback-dir tests/fixtures/feedback/pin-ledger-corpus \
  --out-dir <provenance store outside the checkout>
```
