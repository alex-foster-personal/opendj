# open-dj conformance corpus v0.2

10 curated open-dj JSON documents that exercise the strawman Appendix B
edge cases most likely to regress in adapter implementations.

## Files

| Case | File | track_id | Exercises |
|---|---|---|---|
| 1   | `case-01-classic-isrc.open-dj.json`       | `9bbb11637465090ce8135bcc3e66c0f00cf777fa` | Classic ISRC + ASCII metadata baseline. |
| 1b  | `case-01b-classic-isrc-alt.open-dj.json`  | (USABC1234567-derived)                     | Sibling baseline: different ISRC, second track_id family. |
| 2   | `case-02-unicode-cjk.open-dj.json`        | (JPABC0000001-derived)                     | Japanese title + artist (NFC form). |
| 3   | `case-03-multi-artist.open-dj.json`       | (USABC1111111-derived)                     | 3-element `artists[]` including `feat.` prefix. |
| 4   | `case-04-missing-isrc.open-dj.json`       | (fingerprint-derived)                      | Tier-2 track_id from fingerprint + duration + size. `x_track_id_tier = "fingerprint"`. |
| 5   | `case-05-reencoded.open-dj.json`          | (GBDEF0000001-derived)                     | Re-encoded same content: `track_id` stable, primary `content_hash` + `x_alternate_content_hashes[]` for the MP3 side. |
| 8   | `case-08-cues-loops.open-dj.json`         | (USCUE0000001-derived)                     | 8 hot cues (slots 0..7) + 4 memory cues + loop_in/loop_out pair. |
| 10  | `case-10-provenance-stress.open-dj.json`  | (USPVN0000001-derived)                     | `bpm` is a manual override with 4 prior-source wrappers (mik, rekordbox, djay, serato) under `x_prior_*`. |
| 11  | `case-11-ratings-lossy.open-dj.json`      | (USRAT0000001-derived)                     | `rating = 5` from manual, with `x_prior_serato = 0` -- lossy-lane. |
| 13  | `case-13-play-orders.open-dj.json`        | (USPLA0000001/2)                           | Two-track playlist with two named `play_orders` (warm-up + peak-time). |

Cases 6, 7, 9, 12, 14, 15 are deferred to Phase 16 (`corpus-0.3/`) because
they need Serato/Traktor source data (6, 7, 12) or variable-tempo / streaming
/ Flip edge cases we lack source material for (9, 14, 15).

## Properties enforced by round-trip tests

- Each file's raw bytes are already RFC 8785 JCS canonical.
- Parsing + re-emitting via :func:`apps.open_dj.canon.to_canonical_bytes`
  must be byte-for-byte identical.
- Each file validates against `open-dj/schema/v0.2/open-dj.schema.json`.

See `tests/open_dj/test_corpus_roundtrip.py`.
