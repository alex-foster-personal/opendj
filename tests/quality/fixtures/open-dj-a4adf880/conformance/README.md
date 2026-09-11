# open-dj conformance corpus

Seed corpus for the spec. Each version's corpus lives under `corpus-<VERSION>/`.

## Layout

- `corpus-0.2/` -- first seed corpus (Phase 15, this repo).
  Covers Appendix B cases 1, 2, 3, 4, 5, 8, 10, 11, 13 from the spec.
  The original seed workflow used private library captures. Those inputs are
  unavailable in this public copy; retained protocol examples do not establish
  real-library conformance acceptance.
  Phase 16 adds cases 6, 7, 9, 12, 14, 15 (needs Serato / Traktor / variable-tempo
  source data).

## How to use

From the repo root:

```bash
python -m apps.open_dj.cli validate open-dj/conformance/corpus-0.2/case-01-classic-isrc.open-dj.json
python -m apps.open_dj.cli canon open-dj/conformance/corpus-0.2/case-01-classic-isrc.open-dj.json
```

The tests in `tests/open_dj/test_corpus_roundtrip.py` iterate the whole
directory and assert every file is canon-idempotent and schema-valid.

## Adding a case

1. Pick a qualifying track from the real library (see `data/sync/matches.csv`).
2. Export it via an adapter (`cli export --adapter rekordbox --tracks <stable_id>`).
3. Strip `file_path` to a relative path rooted at `/music/`.
4. Name the file `case-<NN>-<slug>.open-dj.json`.
5. Update `corpus-0.2/README.md` with the case number + source `stable_id`.
