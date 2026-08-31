# Stretch-quality harness (lane agentB)

The table in [`report.md`](report.md) is the gate for the `STRETCH_BLOCK_MS`
flip (latency round 2, step 2). Binding spec:
[`.planning/QUALITY-METHODOLOGY-RECONCILED.md`](../../../.planning/QUALITY-METHODOLOGY-RECONCILED.md),
copied into this branch unchanged so the harness and its spec travel together.

```bash
just stretch-quality                      # fetch, render, analyse, report
just stretch-quality --stages analyse report   # re-score without re-rendering
just stretch-quality-tests                # the analysis primitives
just stretch-quality-smoke                # the CI-runnable render smoke
```

## Shape

| stage | where it runs | what it needs |
| ----- | ------------- | ------------- |
| `fetch_fixtures.py` | Python, stdlib only | the LANE DAEMON (`--base-url`, default `:8685`) |
| `stretch-quality.spec.ts` | REAL Chromium via Playwright | nothing -- no dev server, no port, no data dir |
| `analyse.py` | Python, numpy only | the PCM the render stage wrote |
| `report.py` | Python, stdlib only | `results.json` |

The analysis half is one module per measurement domain, and the amendment that
constrains each is named at its implementation site: `contract` (structural
constants and the refusal hierarchy), `pcm`, `alignment`, `spectral`, `onsets`,
`pitch`, and `probes` (the synthetic material whose shape breaks a naive
measurement). `metrics.py` is the facade that re-exports all of it, so
`metrics.X` is still the one import surface.

The render stage serves the pinned `signalsmith-stretch` 1.3.2 module from
`node_modules` to a synthetic origin through Playwright request interception,
and drives it with the repo's exact `STRETCH_NODE_OPTIONS`, imported from
`stretch-adapter.ts` rather than copied. Decoding is `decodeAudioData` in the
same browser, which is the decoder the deck itself uses, so the excerpt under
measurement is the audio the app would really play.

Fixture extraction goes through the daemon's audio endpoint and never a raw DB
path: path healing lives between the DB and the filesystem, so a `stable_id`
resolved by hand is not the file the app would open.

### Why there is no librosa, and no PEP 723 wrapper around the analysis

The brief's constraint is that heavy deps never enter the repo venv. This
harness satisfies it by not needing any: decoding happens in Chromium, and the
analysis is pure numpy (STFT, envelope carriers, cross-correlation, YIN), which
is already a base dependency. So `scripts/quality/` is a normal importable
package run as `python -m scripts.quality.run`, matching the existing
`scripts/quality_gate.py` precedent, and its primitives are directly testable
under the repo's own pytest with no extra install.

The one place a heavy dep IS wanted -- cross-checking the f0 tracker against
`librosa.pyin` -- uses `uv run --with librosa`, an ephemeral overlay
environment, exactly as the repo already does for modal.

A side benefit that amendment 9 turned into the main one: linear-frequency flux
means the librosa `n_fft=512` / `n_mels=128` dead-low-band defect cannot arise
here at all. **This lane's own-basis verification (amendment 9 clause 2): there
is no mel basis in this harness.** The flux is a 2048-point linear STFT at
44.1 kHz, 21.5 Hz per bin with 11 bins below 250 Hz, and
`assert_low_band_resolution` refuses any FFT size that would blunt the bass
region. The observable property is pinned rather than argued: a 60 Hz kick with
a raised-cosine attack and under 0.1 percent of its energy above 250 Hz is
still found within 50 ms of every hit. The raised-cosine attack matters -- a
hard-gated kick is broadband at its edge, so a detector genuinely deaf to the
low band would still "find" it and the probe would prove nothing.

No onset number in `results.json` was ever produced on the librosa defaults, so
there are no pre-fix rows to regenerate and no pre/post mixing in this table.

## Instrument decisions

Each is a spec amendment, and each was an empirically-found trap rather than a
preference.

| decision | value | why |
| -------- | ----- | --- |
| latency trim | **NONE** | Amendment 3. signalsmith-stretch 1.3.2 in buffer-playback mode already self-compensates its reported latency. Trimming by `node.latency()` puts every render 120 ms early and poisons every metric. The self-report is read after `configure()` and carried as METADATA. |
| ranking metrics | LSD, onset displacement p95, onset recovery | Amendment 1. Residual dBr is a diagnostic only. |
| residual | reported, never ranks | Amendment 1. `residual dBr = 10*log10(1 - rho^2)`, a pure function of waveform-shape similarity. The known-worse `cheaper` preset beats default by ~7 dB on residual while losing 6/6 on LSD. |
| D1 | unity **LSD** | Amendment 2. The default preset scrambles phase at rate 1.000 (residual ~0 dBr, rho ~0.07), so a residual-based D1 disqualifies every build including baseline. |
| LSD floor | 80 dB below the REFERENCE PEAK | Amendment 4. Absolute flooring is dominated by empty-bin numerical noise: the same null test reads 39.7 dB absolute-floored and 1.14 dB relative-floored. |
| onset hop | 64 samples (1.451 ms) | Amendment 5. Must be <= gate/4. librosa's default 512 is 11.61 ms, one quantisation step below a 12 ms gate. The harness RAISES if resolution exceeds gate/4. |
| alignment carrier | half-wave-rectified envelope RISE, both stages, four gates | Amendments 6 and 7 plus the Wed 19 Aug refinement. See below. |
| determinism | **enforced** | Every cell renders twice in-page and the two sha256s are compared. Inequality throws and fails the run. |
| channel check | **distinctness**, not count | A mono-collapsed render still reports two channels. The check compares the render's inter-channel relationship against the source's, and reports "inapplicable" for a genuinely mono source rather than silently passing. |

### Alignment: the four gates, and what calibrated them

Amendment 7 forbids correlating raw waveforms at either stage. Lane A's
adversarial verification measured raw correlation returning **lag 100 for a
true lag of 300 at corr 0.9995** on a pure sine, and **-101 at 0.9996** on a
transient-free crescendo: wrong answers at maximal reported confidence, on
exactly the F2/F6 material classes.

The Wed 19 Aug refinement then found the carrier fix ALONE insufficient: a
rectified envelope keeps ~8.5 percent ripple at 2f and correlation is
scale-invariant, so a carrier-only stack still cycle-skipped the sine at corr
0.999. The working stack is four gates, each a refusal to produce a number
rather than a quality verdict:

| gate | what it is | calibration |
| ---- | ---------- | ----------- |
| (a) | envelope-rise carrier at BOTH stages | amendments 6 and 7 |
| (b) | coarse AND fine bound raise | an argmax on a search bound is a clipped guess |
| (c) | correlation floor **on the carrier**, `0.05` | noise reads 0.005, worst real arm 0.148 |
| (d) | reference self-similarity ceiling `0.95` | catches the tone that survives (a)-(c) |

Plus the degenerate-carrier guard (an unmodulated tone's rise carrier is flat)
and peak dominance (raises only when best and runner-up are INDISTINGUISHABLE).

The probe outcomes are pinned in `tests/quality/test_stretch_alignment.py`
against lane A's reference implementation (7068f21f, 4250c732): **sine RAISES,
crescendo RAISES, noise RAISES, and the positive controls come back exact**
(-300 to -300, +150 to +150). The controls are in the same file as the refusals
on purpose: a guard stack that refuses everything is not an instrument.

**Gate (c)'s value is the single most load-bearing number here, and this lane
got it wrong before the refinement landed.** This harness floored at `0.20`,
chosen by taste, and the grid then measured real align correlations with a
median of **0.180** and a range of 0.060 to 0.358 -- so **102 of 162 real cells
sit below 0.20**. A floor set by taste would have refused 63 percent of the grid
and looked rigorous doing it. A floor is only ever as good as the two
measurements it separates.

Measured on the same 162 cells, for whoever sets the joint thresholds:

| observable | min | median | max | guard |
| ---------- | --- | ------ | --- | ----- |
| `align_correlation` | 0.060 | 0.180 | 0.358 | floor 0.05 |
| `align_self_similarity` | 0.077 | 0.362 | 0.521 | ceiling 0.95 |
| `align_peak_ratio` | - | - | 0.919 | ceiling 0.98 |
| `abs(align_delay_ms)` | 0.09 | 2.72 | 9.52 | - |

That last row is amendment 3 confirmed on real audio rather than argued: if the
self-reported latency needed trimming, these would read near 120 ms, not 2.7.

That last threshold is deliberately loose (`0.98`), and the reason is measured.
The spec warns that on 16th-note material at 103-158 BPM a 120 ms-early render
can alias onto an interior peak without tripping a +-25 ms bound, and that four
of the six fixtures sit in that window. Measured here on a synthetic 16th-note
train at 122 BPM, the one-period alias reads **0.83 against a true peak of
1.00** -- so a ratio tight enough to refuse the alias would refuse ordinary
repetitive dance music. Instead the alias is made an OBSERVABLE: every row
carries `align_correlation`, `align_peak_ratio` and
`align_runner_up_delay_samples`, which is what the spec means by
align_correlation being the discriminating observable on those rows.

There is a residual limitation worth stating plainly: a periodic signal
displaced by exactly one period is genuinely indistinguishable from an
undisplaced one, and no algorithm can report otherwise. The runner-up lag is
what exposes it. A bound check alone cannot prove a trim is correct.

### Pitch: the proposed tracker (amendment 8)

Amendment 8 makes the master-tempo promise a measured item and leaves the
tracker to the lanes to propose. **Lane B proposes a numpy YIN** (cumulative
mean normalised difference, parabolic interpolation, first local minimum below
threshold), for three reasons:

1. It keeps the analysis half numpy-only, so the primitives stay testable under
   the repo's own pytest with nothing extra installed. `pyin` needs the
   `analysis` extra; `crepe` pulls torch.
2. The metric is a **differential of two contours produced by the same
   tracker**, so a systematic tracker bias cancels to first order. What has to
   be accurate is the RATIO, not the absolute Hz.
3. Absolute accuracy is pinned anyway against synthesised tones: within
   **0.1 cents at 110-440 Hz** and 0.38 cents at 880 Hz.

Cross-check against `librosa.pyin` on the real F2/F5 excerpts:

```bash
uv run --with librosa --no-sync python -m scripts.quality.pyin_crosscheck \
    --excerpts ops/quality/stretch/work/excerpts --out ops/quality/stretch/pyin-crosscheck.json
```

Run, not assumed. On the real excerpts the two trackers agree to a median of
**+1.46 cents on F2 and +2.67 cents on F5**, with 85 and 88 percent of jointly
voiced frames inside 20 cents and 2 octave disagreements each. Full result in
`pyin-crosscheck.json`. Octave disagreements are counted separately from tuning
disagreements on purpose: they are a different failure and averaging them into
a percentile would hide both.

Two design consequences worth naming:

- Pitch is measured on the **forward** render, never the round trip. A round
  trip restores the original pitch by construction and would score ~0 for every
  build, measuring nothing.
- The expectation is `semitones * 100` cents and is **independent of rate**.
  That independence IS the master-tempo promise.

One calibration defect was found and fixed while building this: YIN's
absolute-threshold step must take the first LOCAL MINIMUM below the threshold,
not the first sample below it. Taking the first sample stops part-way down the
same valley -- measured **+75 cents** on a 220 Hz tone (tau 191 against a true
200) -- while a plain global argmin instead picks the deeper octave-down valley
at tau 401. Both wrong answers are now pinned by tests.

## Open questions for the lane owners

These are REPORTED, not silently resolved.

**Q1. Lane B's base proposal is not in this repository.** The reconciled spec
adopts it as the skeleton for the condition grid C0-C8, the measurement window,
the reporting format and the self-disqualification clauses D1-D7, and says
"Condition grid C0-C8 stands". That document is not present in the lane
worktree, this branch, or anywhere in git history (searched across all refs).
Three things therefore had to be RECONSTRUCTED, and each is labelled as such
where it lives:

- **The C0-C8 grid** (`scripts/quality/plans/grid.json`). Reconstructed as a
  strict SUPERSET of lane A's shipped six (0.92, 1.00, 1.08, 1.16 at semis 0,
  plus +2 and -2 at unity), anchored on the deck's real fader extremes from
  `PITCH_RANGES` (+-8, +-16 percent), with a combined rate-and-key row for the
  real mixing case. Chosen so the two lanes stay directly comparable on the
  shared six whatever the true C0-C8 turns out to be.
- **The measurement window** (`scripts/quality/analyse.py`). `[0.5, 9.5)` s of
  the 10 s excerpt, so neither the stretcher's start ramp nor its buffer-end
  behaviour reaches a metric.
- **D2-D7.** Only D1 has a stated definition (and only because amendment 2
  re-bases it). The report says DEFINITIONS UNAVAILABLE rather than inventing
  them, and lists the observables in `results.json` that would feed them.

**Q2. The onset recovery gate of 12 ms is PROPOSED, not agreed.** It comes from
lane A's harness. It is a parameter of the metric, not a pass threshold, but it
changes what "recovered" counts as, so it needs to be pinned jointly. It is
reported beside every recovery figure.

**Q3. The `anchor_cheaper` arm is defined but NOT in the shipped table.** It is
lane B's addition: the brief asks for baseline vs blockMs 60 vs blockMs 30, and
the `cheaper` preset is carried as a fourth arm because the spec documents it as
the KNOWN-WORSE build, which makes it a sign check on the ranking metrics
themselves -- if any run ever ranks `cheaper` above `baseline` on LSD, the
instrument is wrong rather than the build. It is excluded from this run because
its cells hung the renderer (see the wedge note below); the arm stays in
`plans/arms.json` and `--arms` selects it. Decide whether it is worth chasing.

**Q5. The render wedge, in case lane A sees it too.** Chromium stops resolving
`OfflineAudioContext.startRendering()` after roughly THIRTY renders in one
document, and once it happens every later cell in that page hangs as well --
observed twice, at cell 31 and cell 30 of otherwise identical runs, with no
error raised anywhere. The harness now tears the page down and rebuilds it at
each arm boundary, which keeps the budget at nine cells; the bounded per-cell
wait is a backstop, not the fix, because abandoning a hung `evaluate` does not
un-wedge the page. With that in place the full 162-cell grid renders in **2.5
minutes with zero failures**. Worth flagging because the failure mode is
silence, not an exception, and a grid that dies at cell 30 of 162 looks exactly
like a slow grid.

**Q4. The remaining structural guards are lane B's, and are not quality
thresholds.** `ALIGN_CORRELATION_FLOOR` (0.05) and
`ALIGN_SELF_SIMILARITY_CEILING` (0.95) are now pinned by the spec, but
`ALIGN_AMBIGUITY_MAX_RATIO = 0.98`, `CARRIER_ACTIVITY_FLOOR = 1e-3`,
`PITCH_CONFIDENCE_FLOOR = 0.75`, `PITCH_MIN_VOICED_FRACTION = 0.10` and the
loud-non-silence levels are still this lane's. They decide whether the
instrument will report a number at all, never whether a build passes.

Two are worth a specific look. `PITCH_MIN_VOICED_FRACTION` is doing real work:
F2 reads **10-11 percent** jointly voiced, i.e. it sits on the floor, so a
slightly higher value would drop the sustained/tonal fixture out of the
master-tempo metric entirely. And the pitch `p95` column carries octave errors
on the most damaged cells -- several F2/F5 `block30` rows read ~1204 cents,
which is one octave to within a rounding error and is the tracker losing the
fundamental, not the stretcher transposing by an octave. The `p50` is the
trustworthy statistic on those rows; `p95` should be read as "how often did the
material stop being trackable", which is itself a quality signal but not a
pitch error.

## What is committed

| path | committed | why |
| ---- | --------- | --- |
| `report.md`, `results.json` | yes | the deliverable, and the sha256 of every measured PCM |
| `pyin-crosscheck.json` | yes | the tracker proposal's evidence |
| `work/` (sources, excerpts, renders, manifest) | **no** | ~730 MB of audio per grid; every file's sha256 is in `results.json`, so the committed artifacts still pin exactly what was measured |

No pass/fail threshold is hard-coded anywhere in this harness beyond the
structural raises above. Thresholds are set jointly across lanes after both
harnesses have run these fixtures at these settings, and they ratchet per the
experiment-round rules.
