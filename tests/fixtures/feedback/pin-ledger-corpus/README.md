# Pin ledger export corpus

Locked feedback store snapshot used to regenerate `docs/threads/user-prompts-pins.jsonl`
without a live engine lane. Sources:

- `comments.json` and `archive-*.json` from silver `com.opendj.desktop` feedback
  (rsync `~/Library/Application Support/com.opendj.desktop/feedback/`).
- Archive rows whose ids still appear in `comments.json` are dropped so
  `scripts.feedback_prompts_export` does not refuse duplicate ids.
- Issue #4088 packet pins missing from that store are appended to `comments.json`.

Refresh:

```sh
uv run --no-sync python -m scripts.feedback_prompts_export \
  --repo . \
  --feedback-dir tests/fixtures/feedback/pin-ledger-corpus \
  --output docs/threads/user-prompts-pins.jsonl
```
