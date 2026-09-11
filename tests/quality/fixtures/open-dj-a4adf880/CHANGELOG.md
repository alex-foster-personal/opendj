# open-dj CHANGELOG

## 0.2 -- 2026-04-17

Promoted from `docs/open-dj-v0-strawman.md` 0.1 to a normative in-repo location.

Changes from 0.1:

- `schema_version` bumped from `"0.1"` to `"0.2"`.
- `$id` base URL added to every schema (`https://open-dj.org/schema/0.2/...`).
  The URL is provisional and will be reassigned when the spec publishes externally
  in Phase 16 (OPEN-03 publish step).
- Clarified: identity fields on `Track` (`title`, `artists`, `album`, `isrc`,
  `duration_ms`, `file_path`, `content_hash`, `track_id`) are raw scalars / arrays,
  NOT wrapped in `ProvenanceValue`. Spec section 6 was ambiguous at 0.1.
- Clarified: every object type in the schema carries
  `patternProperties` with pattern `^x_[a-z0-9][a-z0-9_]*$` so extension keys validate.
- Fixed: `Playlist.play_orders[]` default export strategy (spec section 7.4).
  First entry replaces `tracks_ordered[]`; the rest are dropped with a warning.
- New: in-repo conformance corpus at `open-dj/conformance/corpus-0.2/`,
  exercising Appendix B cases 1, 2, 3, 4, 5, 8, 10, 11, 13 (10 tracks).

## 0.1 -- 2026-04-16

Initial strawman published at `docs/open-dj-v0-strawman.md`.
