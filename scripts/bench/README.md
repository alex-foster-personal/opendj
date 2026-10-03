# vocal_quality_rater.html -- vocal-stem quality rater

Standalone HTML, no build, no CDNs. Rate isolated vocal clips 1-10 against a reference stem.

1. Copy `ladder.json.example` to `ladder.json` and point it at your clips (paths relative to this dir).
2. Serve this dir: `uv run scripts/bench/serve.py 8791` (fetch needs HTTP, not `file://`, and
   `python3 -m http.server` cannot do Range requests so audio will not seek).
3. Open http://localhost:8791/vocal_quality_rater.html -- rate every clip, then Export JSON.

Default view is the machine ranking with the extremes first: BEST, then WORST, then ranks 2
downward, so the two ends sit next to each other for A/B. Every card shows its machine score,
SI-SDR and demucs settings. `Blind mode` in the top toolbar (off by default) hides all of that,
shuffles the clips into anonymous letters, and holds the comparison table back until every clip
has a score. Alongside sits a "What to listen for" panel splitting separation artifacts into
mark-down-for-these and keep-these-they-are-good.

Each player carries a waveform strip drawn from real WebAudio peaks, styled to match the deck
StripWaveform in the web UI. Click anywhere on a strip to seek there and start that clip, which
pauses whatever else was playing: exactly one player is ever audible, reference and instrumentals
included. Waveforms are drawn as soon as the manifest loads, so every strip is a real click target
before anything is played; the peaks come from a separate `fetch()` on a serial queue and are
cached per URL, and every `<audio>` stays `preload="none"` to avoid eager multi-MB
stem buffering across the player list.

A manifest may set `stem` (retitles the page and swaps the "what to listen for" lists for that
source), `blind_default` (opens blind unless the rater has already toggled it on this manifest),
`reference_kind` (names what the reference is) and `mixture_file` plus `mixture_floor_si_sdr`
(renders the unseparated mixture as a REFERENCE with no score chips, never as a graded arm).

Ratings persist to localStorage per track name. Missing audio shows a per-clip "file missing" state.
A clip may add `inst_file` (instrumental/no_vocals path) to also rate the instrumental stem; clips
without `inst_file` only need the vocal rating. Extensions come from the manifest, so `.flac`,
`.m4a` or anything else the browser decodes all work.

# four-stem ladder -- does the config ranking survive drums, bass and other?

The four-stem ladder runs configured separators on a MUSDB18-HQ input track,
keeps all four stems from each arm, and scores them against the supplied true stems.
Provide the dataset and model inputs explicitly; this document does not publish
private listening results or certify input availability.

- `four_stem_ladder.py` -- separates and scores. Six arms: `hdemucs_mmi` at overlap 0 and 0.25,
  `htdemucs` at overlap 0.25, `htdemucs` at overlap 0 separated at 22.05 kHz, an optional
  user-supplied ONNX 4-stem model (`--with-rb7` plus `--rb7-model` or `RB7_ONNX_MODEL`), and
  a user-supplied Spleeter 4stems SavedModel (`--with-rb6` plus `--rb6-model` or `RB6_SPLEETER_MODEL`). Writes `four_stem_ladder.json`.
- `four_stem_clips.py` -- encodes to m4a in `clips-4stem/` and writes ONE MANIFEST PER STEM
  (`ladder_4stem_vocals.json` and siblings) plus the index `ladder_4stem.json`. The rater carries
  one reference per manifest, so a per-stem split is the only shape that can point each clip at a
  true reference.

Three comparison rules:

1. **The raw mixture is a REFERENCE, not an arm.** It is rendered on the page with no score
   chips, and its per-stem SI-SDR is reported as the do-nothing floor. Grading "no separation at
   all" against a separator must remain a separate do-nothing floor.
2. **Every graded clip has a true reference.** No stem is judged blind against nothing.
3. **The manifests open BLIND by default** (`blind_default: true`) and their `params` strings are
   flat, so labels and score chips do not tell listeners the expected grade.

Record the metric spread between arms before listening. Do not infer audible
differences from a narrow score range or publish a listener verdict without its inputs.

# model shootout -- which separator should production use?

Multi-track, one-rung-per-model comparison against supplied true MUSDB18-HQ vocal
stems. It compares models across tracks rather than only separator knobs on one
song. The experiment spec declares the planned sample; each generated report must
state how many tracks actually completed. No private owner verdict is included here.

- `shootout_spec.py` -- the experiment spec: the twelve tracks, the seven models, the knobs, and
  each checkpoint's training data. Keep the planned sample fixed before collecting
  scores, and report deviations explicitly. Stdlib only so consumers can import it.
- `run_shootout_cuda.py` -- PEP 723 runner for any CUDA host. Picks each excerpt window on the
  TRUE vocal stem, slices the identical sample range from mixture and truth, separates, scores,
  and writes `model_shootout.json` after every track. Refuses to run on CPU.
- `separation_metrics.py` -- SI-SDR plus the under-separation checks (BSS Eval v4 SIR/SAR,
  correlation with the mixture, log-spectral distance). Use this over `si_sdr_score.py` when the
  question is "is this model actually separating", not just "how close is it".
- `shootout_report.py` -- turns the JSON into `MODEL-SHOOTOUT.md`. Cheap, so re-cut the analysis
  freely without touching a GPU.
- `shootout_clips.py` -- encodes the top test-set-clean models on two tracks to m4a in
  `clips-shootout/` with a rater-shaped `ladder.json`, so the metric can be checked by ear.
  These are generated outputs; create them from your configured inputs rather than
  assuming a published capture directory exists.

Two of the seven models (`mdx_extra`, `mdx_extra_q`) were trained with the MUSDB test set
included, per the demucs README. Their scores on this benchmark are contaminated and the report
ranks them separately. Do not quote them as quality numbers.

Running on a configured CUDA host:

Provide your own dataset root and output directory to `run_shootout_cuda.py`
through `--musdb-dir`, `--out-dir` and `--json-out`. Transfer the runner and its
`shootout_spec.py`, `pick_vocal_window.py` and `separation_metrics.py` dependencies
to that host through your deployment tooling. Dataset/model licensing and actual
capability remain operator prerequisites; no private host or store is implied.

# kpi_ledger.json + kpi_append.py + kpi_chart.html -- demucs farm KPI tracking

Columns-over-time ledger schema for the auto-research/auto-hardening loop: a
`kpis` dict (label, unit, direction, hover title) and a `snapshots` array (one run each).
The tools expect an explicitly supplied authorized `kpi_ledger.json` beside the
script; the private captured ledger is not provided here. Missing input fails
rather than creating a successful snapshot. Commands below describe capability
with that prerequisite, not acceptance evidence.

- Append a run: `uv run scripts/bench/kpi_append.py --label <run> --set key=value [--set ...]`.
  Unknown keys hard-error (lists valid keys); unset keys record null; prints the KPIs-by-snapshot table.
- View: `python3 -m http.server` in this dir, open http://localhost:8000/kpi_chart.html
  (fetch needs HTTP, not `file://`). Small-multiples grid, one sparkline per KPI, latest value big,
  green/red arrow for direction-aware good/bad moves. Hover any number for its meaning.
