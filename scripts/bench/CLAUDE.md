# bench/ - agent instructions

Quality benchmarking for stem separation: listening sets, SI-SDR scoring, the KPI ledger, and the
human rating page. The lyrics sibling ledger is `lyrics_kpi_ledger.json`, appended via
`lyrics_kpi_append.py`.

## Reference audio availability

Listening clips and reference audio are not supplied by this public copy. Acquire any required
inputs through a licensed source and record their provenance, checksums, corpus scope, and
acceptance impact before benchmarking. No private archive or acquisition route is available here.

## Non-obvious things that have burned people here

- **Serve with `uv run scripts/bench/serve.py <port>`, never `python -m http.server`.** The latter
  is single-threaded with no HTTP range support, so audio will not seek and later clips silently
  fail to load.
- **Reading an evicted file materialises it permanently.** Any input population for this
  operation requires independently verified provenance; a private pool snapshot is not supplied
  here. `input_pipeline_bench.py` refuses to run without an explicit flag for this reason.
- **Test tracks must pass `stem_content_gate.py`.** A stem can be present, active, and still
  unratable if it sits far below the loudest stem in the same window. Absolute level is not enough.
- **MUSDB alone does not establish performance on another corpus.** A
  four-stem conclusion requires evidence for the intended input population.
- State the corpus, scorer, denominator, and limitations for every result.
  Private operator method notes are not available in this public copy.
