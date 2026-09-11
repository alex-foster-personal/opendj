# Cross-source equivalence: proving two sources' fields are apples-to-apples

Runnable equivalence procedures establish that source fields mean the same
quantity and unit before comparing or applying precedence.

Module: `apps/equivalence/`. Consumer-side gate: `apps/shared/equivalence.py`.

## The standing rule

> **No field feeds the precedence policy without a PASSING verdict.**

A mapping that has not passed an equivalence test is UNVERIFIED. It must not
reach `apps/tags/unify.py` / `docs/tag-unification.md`, and `apps/mik/load.py`
will not write it to `track_fields`. `untested` and `failed` block identically:
the gate is fail-closed, and an absent verdict file blocks everything. The only
way past it is `--i-know-equivalence-is-unverified`, which logs a WARNING naming
every field it waves through and stamps `overridden` on the
`analysis_field_verification` row, so a forced write is never invisible.

Corollary, and it is the part that gets forgotten: **record the test, not just
the result.** A bare agreement percentage cannot distinguish a regression from
a re-measurement. Retain each run verdict with its denominator, normaliser and
reason code.

## What it is for

An agreement rate is meaningless until you have established that the two
fields mean the same thing, in the same units, on the same scale. The failure
is silent in both directions: a HIGH rate can hide a mapping error, and a LOW
rate is AMBIGUOUS (either one source is wrong, or the fields are not the same
field, and the numbers cannot say which).

So this suite produces a per-field VERDICT, not a percentage. A field is
`passed` only if the scale probe found no mismatch, the normaliser is total
over the observed domain, and no systematic-offset cluster remains.

## Run it

```sh
python -m apps.equivalence run           --data-dir data          # verdicts + report
python -m apps.equivalence probe         --data-dir data --json   # step 1 only
python -m apps.equivalence known-answers --data-dir data --json   # step 5 only
python -m apps.equivalence run           --data-dir data --no-write   # dry run
python -m apps.equivalence run           --data-dir data --no-fuzzy   # exact-path pairs only
```

Known-answer commands require an authorized, reviewed fixture and its locked
sibling manifest. Select your input with `--fixture PATH`; the production loader
checks `equivalence-known-answers/v1` and the manifest SHA256, and missing or
mismatched inputs fail rather than being skipped. Excluded private captures are
not supplied or replaced by this document.

Outputs, all under `<data-dir>/state/`:

| Path | What |
|---|---|
| `equivalence-verdicts.json` | the gate file `apps.shared.equivalence.EquivalenceGate` reads |
| `equivalence-report.md` | the same content, human-readable |
| `equivalence-disagreements/<field>-disagreements.csv` | EVERY disagreeing row, never a sample |

Exit codes: 0 clean, 2 a field FAILED (a proven mapping bug), 3 a known-answer
check failed or a fixture track did not resolve, 4 a source could not be read.
An UNTESTED verdict is NOT an error exit: "we have not proven this yet" is a
legitimate steady state, and the gate already refuses to write those fields.
Both stores are opened `file:...?mode=ro`; MIK is read-only to us.

### The audio-backed probes, which are NOT part of `run`

Two questions cannot be answered from the two databases at all, because the
evidence is the audio: what MIK's undocumented `ZSONG.ZVOLUME` measures, and
which source errs on the residual key disagreements. Those live in separate
PEP 723 scripts, deliberately outside `run` and outside the repo venv, because
they cost a full decode per track and their answer changes on a timescale of
months, not per commit.

```sh
scripts/mik_audio_resolve.py   --data-dir data                 # who has audio, and how we found it
scripts/mik_volume_identify.py --data-dir data --sample 400    # what ZVOLUME is
scripts/key_third_opinion.py   --data-dir data --control 120   # a third key vote
```

| Path | What |
|---|---|
| `equivalence-audio-map.json` | every MIK row resolved to audio on disk, with its resolution tier and its file mtime against `ZANALYSISDATE` |
| `equivalence-loudness-probe.json` | five candidate loudness quantities per track plus the fit of each against `ZVOLUME` |
| `equivalence-key-third-opinion.json` | the chroma key estimate per disagreeing track, its cluster, and the calibration control |

**The resolvable population is volatile and must be re-measured.** Report each
run's input identity, resolution tiers and measured denominator. File moves,
cloud offloading and changed audio bytes can invalidate an earlier measurement;
this publication contains no private library census or repair history.

## The five steps, and where each lives

| Step | Module | What it does |
|---|---|---|
| 1 range and cardinality probe | `probe.py` | full-column min/max/distinct/histogram per source per field, plus a mechanical declared-unit plausibility check and a cross-source ratio check |
| 2 total normalisers | `normalisers.py` | every value maps to a canonical value, to `MISSING` via a DOCUMENTED sentinel, or RAISES |
| 3 post-normalisation agreement | `compare.py` | rate with its denominator named, plus a CSV of every disagreeing row |
| 4 systematic-offset detection | `compare.py` | names the delta (semitone, relative-key hop, exactly 2x, exactly 100x, constant additive) and classifies it MAPPING BUG or SUSPECT |
| 5 known-answer harness | `known.py` | a declared known-answer fixture with `source-read` / `audit-table` / `needs-maintainer` provenance per expectation |

Key mapping is DELEGATED to `apps/shared/harmonic.py::key_to_camelot`, which
already handles Camelot and musical notation including enharmonics and raises
on unknown input. `normalisers.py` adds only a total pre-pass in front of it
for open-key notation, the `maj`/`min` word suffixes, and sentinels. There is
deliberately one key table in this repo, not two.

## Every normaliser, its convention, and where the convention comes from

A normaliser is TOTAL over its declared unit: every input maps to a canonical
value, maps to `MISSING` via a sentinel that was OBSERVED in the real column, or
RAISES `NormaliseError`. There is no fourth branch, and in particular no
pass-through. `tests/test_equivalence_normalisers.py` fuzzes each one to prove
nothing escapes as a bare `TypeError`/`OverflowError`.

Canonical unit per kind (`normalisers.KIND_CANONICAL`), and the recorded
`normaliser` string in the verdict file is always `<kind>:<unit>-><canonical>`:

| Kind | Canonical form | Convention | Source of truth for that convention |
|---|---|---|---|
| `key` | `Key(pitch_class 0-11, mode)`, notation-free | Camelot `1A` is A-flat minor (pitch class 8), `1B` is B major (11), and `+1` on the wheel is a perfect fifth (`+7` semitones). Comparison is categorical equality on `(pitch_class, mode)`, never on the string | The Camelot wheel as implemented once in `apps/shared/harmonic.py::key_to_camelot`; `normalisers.py` only pre-passes open-key and word suffixes into it |
| `bpm` | beats per minute, float | `bpm` x1, `centi_bpm` x0.01, `milli_bpm` x0.001. Legal input windows 20-400, 2,000-40,000, 20,000-400,000 respectively, checked BEFORE scaling so a centi-BPM column declared as `bpm` fails at step 1 | `config.py` declares rekordbox `centi_bpm`; verify the input unit before comparing. `state.db` divides by 100 on ingest |
| `energy` | `energy_1_10` | 1-10 integer scale, no rescaling applied between `energy_1_10` and `energy_0_4`. They share factor 1.0 precisely BECAUSE no constant reconciles an index with a level; a cross-unit energy pair is a declaration bug to be caught, not silently scaled | Declared scalar and series energy unit is 1-10. Probe actual inputs and check scalar containment against its own series |
| `loudness` | `db_same_family_only` | dBFS peak, RMS dB, LUFS integrated and LUFS short-term all carry factor 1.0 and a cross-family pair is REFUSED by `units_incompatible()` rather than scaled. Four different measurements wearing one word; no constant converts between them | ITU-R BS.1770 (LUFS) vs peak/RMS dB are different quantities by definition. Do not infer a loudness family from its column name. The configured `ZVOLUME` unit is `rms_db`; empirical unit identification still needs declared real inputs and retained measurements, see below |
| `duration` / `time` | milliseconds | `seconds` x1000, `samples_44100` x1000/44100, `samples_48000` x1000/48000. Series are stored time-indexed in ms | `docs/terminology-reference/time-series-vs-scalar.md` |
| `rating` | stars 0-5 | `stars_0_5` x1. `rekordbox_popm_0_255` accepts ONLY `0/51/102/153/204/255` and raises on anything else, because a linear 0-255 read would invent half-stars. `linear_0_255` (a genuinely linear column, if one ever turns up) is x5/255 | ID3v2 POPM defines the file byte; the declared database rating scale is `stars_0_5`, distinct from the file form |
| `count` | plain event count | x1. Registered as a real unit so a source-unique count still passes through a total normaliser instead of being trusted raw. `0` is a measurement, never a sentinel | Probe actual `ZCLIPPEDPEAKCOUNT` inputs; zero is a genuine count, not a missing sentinel |

Two categories of unit are deliberately not values:

- `ABSENT_UNITS` (`absent`) -- "this source has no such column". Not an error,
  not a value; it yields `untested_no_data`.
- `UNDECIDABLE_UNITS` (`unknown`, `unknown_db_family`) -- the family is knowable
  but the member is not. Guessing is the failure mode this suite exists to
  prevent, so the verdict is UNTESTED.

Sentinels are registered per `(kind, unit)` only with documented input evidence.
Configured sentinels include BPM and duration zero, MIK key `'0'`, and rekordbox
`ScaleName` `'All'`. Do not invent additional sentinels: that hides unmapped values.
Energy has no sentinel. Detect absence of analysis using tempo/key/segment
availability rather than discarding a legitimate energy value.

## Reading run results

Run the suite against your declared inputs and retain its own denominators,
normalizers, match tiers and reason codes. No private library run results are
published here. This document does not grant any field a PASSING verdict.

A non-passing verdict reports `basis: unverified`, never `cross_source`. Having
run a comparison and found it inconclusive is not evidence that two sources
were cross-validated, and the consumer gate rejects an entry that claims
otherwise.

The reason codes are deliberately distinct because ABSENT is not FAILED.
`untested_no_data` means one side has no data and there is nothing to fix.
`untested_proxy_field` means the two sides are not the same quantity.
`inconclusive_suspect_cluster` means the columns are comparable but patterned
disagreement remains unadjudicated. Only a real unit or convention error
yields `failed`.

## Source-unique fields, and the `basis` field

Four MIK fields have no rekordbox counterpart at all, so no agreement rate can
exist for them: the energy SCALAR (`energy`), MIK's loudness (`loudness`), the
energy TIME SERIES (`energy_segments`) and MIK's clipping count
(`clipped_peak_count`). Blocking
them for want of an agreement rate would lose the only copy of the series
anywhere in the toolchain, so the suite emits verdicts for them on a narrower,
explicitly-labelled basis.

For a source-unique field the suite runs step 1 (full-column range and
cardinality probe) and step 5 (normaliser totality), OMITS step 3
(cross-source agreement) because there is nothing to agree with, and for a time
series adds the structural invariants a scalar does not have.

### Declare and index source-unique fields explicitly

Use `SingleSourceField` for a quantity with no counterpart, rather than a
`FieldPair` against an absent column. The known-answer target index must include
both field lists, and unresolved checks must not count as successful checks.
A source-unique field with an undecidable unit remains untestable: both the
field shape and unit need an explicit, evidenced declaration.

### The one cross-check a source-unique scalar CAN have

A scalar with a companion series in the same source is not entirely
unverifiable. `audit_scalar_against_series` uses the only leverage available:

- **Containment gates the verdict.** The scalar must lie within its own series
  range. Escaping it returns `failed` / `failed_mapping_bug`; source uniqueness
  does not waive this check.
- **Derivability is informational.** Compare named hypotheses such as maximum,
  mode, median and weighted percentiles with measured denominators. Do not
  assume the scalar reconstructs the series, or infer redundancy from one fit.

**Exact spellings the consumer must match** (`apps/equivalence/single_source.py`):

| Key | Values |
|---|---|
| `status` | `passed` / `failed` / `untested`, unchanged, so the existing gate needs no new enum |
| `basis` | `cross_source`, `single_source`, or `unverified` when nothing was verified |
| `suite_status` | `passed_single_source` for a single-source pass, `failed_structure` for a broken series |
| `shape` | `scalar` or `time_series` |
| `agreement` | always `null` for a single-source field, with `agreement_omitted_because` saying why |

A single-source pass is deliberately NOT spelled `passed` in `suite_status`. It
is weaker evidence than two-source agreement, and nobody reading the verdict
file in six months should be able to mistake a one-sided probe for
cross-validation.

## Identifying a source loudness unit

A name such as `ZVOLUME` does not establish a measurement family.
`scripts/mik_volume_identify.py` compares candidate quantities from actual audio:
sample-peak dBFS, RMS dB, true-peak dBTP, integrated LUFS and loudness range.
Record Pearson correlation, fitted offset and residual error for each candidate.
High correlation alone does not establish identical units.

Check that the audio stream is the one the source analyzed: file modification
time alone is weak because tag writes can alter it; source bitrate and analysis
date provide additional provenance. Name subset denominators explicitly, and
retain calibration/control results rather than silently selecting a better fit.

An analysis-window or encode mismatch can make same-family values differ. Do
not apply a fitted correction constant without proportionate retained evidence.
`units_incompatible()` refuses unlike loudness families. Current configuration
is a declaration, not this document certifying measurements on your library.

## Mapping and measurement checks

- Check source BPM scale before joining. The declared rekordbox unit is
  `centi_bpm`; declaring it plain BPM must fail the input-range probe.
- Normalize mixed musical/Camelot key notation through the shared harmonic
  parser. A raw string disagreement need not be a musical-key disagreement.
- Keep database stars distinct from the ID3 POPM byte; do not divide a 0-5
  database rating by the file scale.
- Convert energy/time-series boundaries, not separately rounded starts and
  lengths: `start_ms = round(start_s * 1000)`,
  `end_ms = round((start_s + length_s) * 1000)`,
  `length_ms = end_ms - start_ms`. Check overlaps and non-positive spans.
- Treat half/double BPM as one signature across both directions. A cluster
  alone does not establish a mapping bug or decide which detector is right.
- Calibrate any third detector against a named control before using its vote
  to arbitrate disagreements. Report fuzzy-match tiers separately: an incorrect
  pair can produce an apparent source disagreement.
- File existence is insufficient for cloud-offloaded media; retain actual
  decoder outcomes and unavailable-input counts.
- Analysis spans and container duration are distinct quantities. Near agreement
  does not prove equivalence. Clamp pre-zero analysis windows explicitly and
  record the clamp rather than interpreting an offset as corruption.
- Exclude documented sentinels when testing cardinality collapse. An absent
  value is not a second notation. A constant rating column cannot establish
  agreement; label that percentage an artefact, not a passing verdict.

These are procedures and interpretations, not private run outcomes or an owner
precedence decision. Retain per-run inputs, denominators and failure evidence.

## Extending it to a new source

1. Add a reader to `sources.py` returning a flat row dataclass.
2. Declare each candidate column in `config.py` as a `SourceField` with its
   `kind` and its DECLARED `unit`, and record what you MEASURED in the `note`.
3. If the unit is new, register it in `normalisers.py` with its conversion
   factor and its legal input window. Do not guess a unit: an unknown unit is
   `unknown_db_family` or similar, and the correct verdict is UNTESTED.
4. Add sentinels only for values you have OBSERVED. An invented sentinel turns
   a genuinely unmapped value into `MISSING` and hides the bug.
5. Add fixture entries for tracks you can verify. Mark anything you cannot
   settle as `needs-maintainer` with an empty expectation rather than guessing.

## The rule, restated where it is easy to find

**No field feeds the precedence policy without a passing verdict.** A mapping
without a passing equivalence test is UNVERIFIED and must not reach
`docs/tag-unification.md` / `apps/tags/unify.py`, and `apps/mik/load.py` will
refuse to write it. Record the test, not just the result, so the next agent can
tell a re-measured disagreement from a regression.

Passing fields and unresolved known-answer expectations must come from the
current run and declared fixture, not a historic library summary in this document.
Provide authorized inputs to the known-answer harness; unresolved expectations
stay unresolved. No excluded private capture is implied to be available.

See also: `docs/analysis-retention.md` (consumer-side retention policy),
`docs/terminology-reference/time-series-vs-scalar.md`.
