# Serato history session captures

## What this is

Real `_Serato_/History/Sessions/*.session` files written by Serato DJ, committed
byte-for-byte so `apps/adapters/serato/history.py` has a test through Serato's real
format (AGENTS.md external-boundary rule). The synthetic sessions in
`tests/adapters/serato/test_history.py` cover edge cases. They share the parser's
assumptions, so they cannot prove those assumptions.

## Status

No capture is committed yet. Until one is, `test_committed_captures_parse_to_their_oracle`
reports UNAVAILABLE, which means unmeasured, not passed.

## Capturing

Operator with Serato DJ:

1. Put 3 or more royalty-free or self-made tracks on an external volume named
   `FIXTURE`, so every path is `/Volumes/FIXTURE/...` and no home directory or user
   name can get into the file.
2. In Serato start a session. Load and play at least two tracks past Serato's played
   point. Load one more track and eject it without playing. End the session.
3. Copy the newest file from `_Serato_/History/Sessions/` without opening or
   re-saving it.
4. In Serato's History panel export that session (CSV). That export is the **oracle**.
   Copy every row's file path, start time (as Unix seconds) and played state into the
   manifest `entries`. Never run our parser to fill in the expected values.
5. Check that the file carries nothing personal: `strings -el <file>` shows only
   `FIXTURE` paths and track metadata, with no `/Users/`, `/home/`, `C:\Users\` or
   user names. If anything personal shows up, capture again from a clean setup. **Do
   not edit the bytes**: a hand-edited file is no longer a capture.
6. Record `sha256sum <file>` in the manifest and commit the file next to this README.

## Manifest schema

Each entry in `manifest.json` `captures`:

```json
{
  "file": "<name>.session",
  "sha256": "<hex of the committed bytes>",
  "serato_version": "Serato DJ Pro 3.x.y",
  "os": "macOS 14.x",
  "captured": "YYYY-MM-DD",
  "oracle": "Serato History panel CSV export of the same session",
  "entries": [
    {"index": 0, "path": "Volumes/FIXTURE/Music/01.mp3", "start_s": 1740000000, "played": true}
  ]
}
```

`entries` lists every `oent` in file order, unplayed ones too. `start_s` is Unix seconds.
`path` is the path exactly as Serato's own export shows it.

## Rules

One manifest entry per `.session` file in this folder (the guard test enforces it). A
replaced file fails on its sha256. Never change `entries` to match parser output: if the
parser disagrees with the oracle, the parser is wrong.
