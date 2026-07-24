# Vocal separation model shootout

6 MUSDB18-HQ test tracks, 7 models, 60s vocal-dense excerpt per track, scored against the TRUE vocal stems.
All models run at identical knobs (overlap 0.25, shifts 0) on a NVIDIA GeForce GTX 1660, so the model is the only variable.

Primary metric is SI-SDR in dB against ground truth, higher is better. The do-nothing baseline (the mixture graded as if it were a vocal estimate) has a median of -3.10 dB and is the deliberately bad arm: a test that cannot tell it from a real separator is broken.

## Read this before the tables: two of these models saw the test set

The demucs README states that `mdx_extra` was "trained with extra training data (**including MusDB test set**)", and `mdx_extra_q` is its quantized twin. Every track below is a MUSDB18-HQ **test** track, so those two models are being graded on material they trained on. Their scores measure memorisation as well as separation and CANNOT be compared with the others.

`mdx` is the same architecture trained on the train split only, so the `mdx` versus `mdx_extra` gap is the cleanest available read on how much the leak is worth. The headline below is therefore taken from the uncontaminated models only; the leaked pair is reported as evidence, not as a recommendation.

## Headline

- Best median quality among test-set-clean models: **hdemucs_mmi** at 9.87 dB (min 4.16, max 16.68), winning 0 of 6 tracks outright.
- Best quality per second among clean models: **hdemucs_mmi** at 4.12 dB/s (9.87 dB in 2.40s median) on NVIDIA GeForce GTX 1660, a figure that must be re-measured on the production GPU before it is used.
- Top of the raw board is **mdx_extra** at 10.30 dB, which is one of the leaked models. Treat that as a measurement of train-on-test contamination, not of quality.
- Under-separation flags: **mdx_extra_q** (see the honesty check below).
- Sanity anchor: every model beat the do-nothing mixture baseline, so the test discriminates.

## SI-SDR by track and model (dB vs true vocals)

| track | genre | mdx_q | mdx | mdx_extra_q (leak) | mdx_extra (leak) | htdemucs | htdemucs_ft | hdemucs_mmi | winner | winner, clean only | mix baseline |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Al James - Schoolboy Facination | hip-hop, rapped male lead | 6.42 | 6.16 | 9.12 | 9.17 | 6.43 | 7.05 | 6.94 | **mdx_extra** | **htdemucs_ft** | -0.38 |
| Zeno - Signs | pop rock, sung male lead | 9.15 | 9.27 | 10.19 | 10.37 | 9.36 | 8.96 | 9.78 | **mdx_extra** | **hdemucs_mmi** | -3.23 |
| Timboz - Pony | metal, screamed male over dense guitars | 3.83 | 3.72 | 4.63 | 4.63 | 4.13 | 4.19 | 4.16 | **mdx_extra** | **htdemucs_ft** | -8.38 |
| Sambasevam Shanmugam - Kaathaadi | Tamil film, non-Western instrumentation | 15.20 | 15.24 | 17.27 | 17.45 | 16.38 | 16.47 | 16.68 | **mdx_extra** | **hdemucs_mmi** | 0.56 |
| Enda Reilly - Cur An Long Ag Seol | Irish-Gaelic folk, sparse acoustic | 12.31 | 12.19 | 13.06 | 13.70 | 12.86 | 13.44 | 13.30 | **mdx_extra** | **htdemucs_ft** | -3.01 |
| Cristina Vane - So Easy | blues, female over slide guitar | 9.08 | 9.03 | 10.00 | 10.23 | 9.68 | 10.08 | 9.96 | **mdx_extra** | **htdemucs_ft** | -3.19 |
| **median** |  | **9.11** | **9.15** | **10.10** | **10.30** | **9.52** | **9.52** | **9.87** |  |  | -3.10 |
| **mean** |  | **9.33** | **9.27** | **10.71** | **10.92** | **9.81** | **10.03** | **10.14** |  |  | -2.94 |

The `mix baseline` column is that track's SI-SDR if you skipped separation entirely. Tracks where it is high are ones where the vocal already dominates the mix, so every model looks good; tracks where it is low are the real test.

## Ranking by median SI-SDR, with spread

| rank | model | median SI-SDR | mean | min | max | spread | wins | test-set clean | training data |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| 1 | mdx_extra | **10.30** | 10.92 | 4.63 | 17.45 | 12.82 | 6/6 | **NO, saw test set** | MusDB train + extra INCLUDING MusDB test set |
| 2 | mdx_extra_q | **10.10** | 10.71 | 4.63 | 17.27 | 12.65 | 0/6 | **NO, saw test set** | MusDB train + extra INCLUDING MusDB test set (quantized) |
| 3 | hdemucs_mmi | **9.87** | 10.14 | 4.16 | 16.68 | 12.53 | 0/6 | yes | MusDB train + 800 songs |
| 4 | htdemucs_ft | **9.52** | 10.03 | 4.19 | 16.47 | 12.29 | 0/6 | yes | MusDB train + 800 songs, per-source fine-tuned |
| 5 | htdemucs | **9.52** | 9.81 | 4.13 | 16.38 | 12.25 | 0/6 | yes | MusDB train + 800 songs |
| 6 | mdx | **9.15** | 9.27 | 3.72 | 15.24 | 11.52 | 0/6 | yes | MusDB HQ train only |
| 7 | mdx_q | **9.11** | 9.33 | 3.83 | 15.20 | 11.37 | 0/6 | yes | MusDB HQ train only (quantized) |

Spread is max minus min across tracks. A large spread means the model is material-dependent and a single-track measurement of it proves nothing.

## Quality per unit compute

| model | median SI-SDR | median separate_s | x cheapest | dB per second |
| --- | ---: | ---: | ---: | ---: |
| hdemucs_mmi | 9.87 | 2.40 | 1.00x | **4.12** |
| htdemucs | 9.52 | 3.83 | 1.60x | **2.48** |
| mdx | 9.15 | 8.73 | 3.65x | **1.05** |
| mdx_extra | 10.30 | 9.86 | 4.12x | **1.04** |
| mdx_q | 9.11 | 8.80 | 3.68x | **1.04** |
| mdx_extra_q | 10.10 | 9.84 | 4.11x | **1.03** |
| htdemucs_ft | 9.52 | 15.02 | 6.27x | **0.63** |

All timings come from a **NVIDIA GeForce GTX 1660**. `separate_s` is GPU inference time only and excludes model loading, which production pays once. It is reported as a MEDIAN across tracks on purpose: the first transformer model to run in a fresh process absorbs one-off cuDNN kernel selection, which inflated a single measurement by roughly 4x on track one. A median over 6 tracks is immune to that; a mean would not be.

**These ratios do not transfer across hardware.** `htdemucs` and `htdemucs_ft` are hybrid transformers while `hdemucs_mmi` and the mdx family are pure convolutional, and those two shapes scale differently on different GPUs: transformer attention gains far more from modern tensor cores than stacked convolutions do. A cost ratio measured on the Turing-generation card above can compress, or invert, on the Ada-generation L4 that production would use. Treat the dB-per-second column as evidence that a large gap exists on THIS hardware, and re-measure the shortlisted models on the production GPU before any cost figure is used to justify a model choice.

## Honesty check: is any SI-SDR win bought by under-separating?

SI-SDR does not punish leftover accompaniment as harshly as it punishes distortion, so a cautious model can score well by barely separating. SIR (signal to interference ratio, BSS Eval v4 with both true sources supplied) measures exactly the accompaniment that leaked through, and correlation with the mixture catches an estimate that is mostly just the input again. A model is flagged when it ranks at least 2 places better on SI-SDR than on SIR AND sits above the cross-model median mixture correlation.

| model | SI-SDR rank | SIR rank | rank gain | median SIR dB | median SAR dB | corr with mixture | verdict |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| mdx_extra | 1 | 3 | +2 | 18.76 | 11.35 | 0.6002 | clean |
| mdx_extra_q | 2 | 5 | +3 | 18.23 | 11.24 | 0.6028 | **FLAG** |
| hdemucs_mmi | 3 | 2 | -1 | 18.84 | 10.77 | 0.5957 | clean |
| htdemucs_ft | 4 | 1 | -3 | 19.74 | 10.65 | 0.5893 | clean |
| htdemucs | 5 | 4 | -1 | 18.38 | 10.51 | 0.5968 | clean |
| mdx | 6 | 6 | +0 | 17.63 | 10.30 | 0.6024 | clean |
| mdx_q | 7 | 7 | +0 | 17.50 | 10.27 | 0.6018 | clean |

Flagged models should not be adopted on SI-SDR alone. Read the flag together with the SAR column before concluding anything, because two very different models trip this rule. A flagged model with a LOW SAR is the bad case: it is leaving accompaniment in and adding artifacts, and its SI-SDR is hollow. A flagged model with a HIGH SAR is the interesting case: it separates less aggressively but what it returns is cleaner, which is a real trade (less bleed-through removal, fewer swirly artifacts) and often the more usable stem in a DJ context. Either way, audition it.

## Perceptual proxy: log-spectral distance (lower is better)

| model | median LSD dB | median SAR dB | median SIR dB |
| --- | ---: | ---: | ---: |
| mdx_extra | 6.19 | 11.35 | 18.76 |
| mdx_extra_q | 6.33 | 11.24 | 18.23 |
| htdemucs_ft | 6.35 | 10.65 | 19.74 |
| htdemucs | 6.60 | 10.51 | 18.38 |
| hdemucs_mmi | 6.72 | 10.77 | 18.84 |
| mdx | 7.32 | 10.30 | 17.63 |
| mdx_q | 7.43 | 10.27 | 17.50 |

LSD is scale-aligned before comparison. It agrees with SI-SDR only loosely, which is expected: LSD is blind to phase and weights quiet bins equally, so treat it as a second opinion rather than a tie-breaker.

## The production decision

Cheapest test-set-clean model is `hdemucs_mmi` (2.40s median). The strongest alternative worth paying for is `htdemucs_ft` (15.02s, 6.3x more GPU time).

|  | `htdemucs_ft` (rival) | `hdemucs_mmi` (cheapest) | delta |
| --- | ---: | ---: | ---: |
| median SI-SDR dB (unpaired) | 9.52 | 9.87 | +0.35 |
| **median PAIRED delta dB** | reference | -0.07 | **-0.07** |
| median SIR dB | 19.74 | 18.84 | -0.90 |
| median SAR dB | 10.65 | 10.77 | +0.12 |
| median separate_s | 15.02 | 2.40 | 6.3x cheaper |
| GPU hours for 1100 tracks | 4.6 | 0.7 | 3.9 saved |
| per-track wins | 0/6 | 0/6 |  |

Paired per-track deltas against `htdemucs_ft`, the correct statistic for this design because every model separated the identical excerpts. The unpaired median row above is shown only so the two can be compared: track difficulty spans roughly 4 dB to 17 dB here, far wider than any gap between models, so a difference of medians can and does reverse the per-track sign.

| model | median delta vs `htdemucs_ft` | min | max | tracks beating `htdemucs_ft` |
| --- | ---: | ---: | ---: | ---: |
| `hdemucs_mmi` | **-0.07** | -0.14 | +0.82 | 2/6 |
| `htdemucs` | **-0.25** | -0.61 | +0.40 | 1/6 |
| `mdx` | **-0.97** | -1.25 | +0.31 | 1/6 |
| `mdx_q` | **-0.82** | -1.27 | +0.19 | 1/6 |

**Verdict: use `hdemucs_mmi`.** It gives up 0.07 dB of median SI-SDR, which is under the 0.2 dB threshold this project treats as inaudible, and it is 6.3x cheaper. Across 1100 tracks that is 3.9 GPU hours saved. Per track the paired difference stayed within -0.14 to +0.82 dB over 6 tracks, so this is a median with no bad case hiding behind it.

This is a metric-level verdict on one GPU generation. Confirm it by ear (clips not yet generated: run `shootout_clips.py`), and re-measure the timings on the production GPU, before committing a full catalogue run.

## Sizing the train-on-test leak, and the quantization cost

Same architecture, different training data or precision, so each pair isolates one variable.

| pair | what it isolates | median paired delta | range | tracks favouring the right-hand model |
| --- | --- | ---: | ---: | ---: |
| `mdx` vs `mdx_extra` | MusDB test set in the training data | **+1.36 dB** | +0.91 to +3.01 | 6/6 |
| `mdx_q` vs `mdx_extra_q` | MusDB test set in the training data (quantized) | **+0.98 dB** | +0.75 to +2.70 | 6/6 |
| `mdx` vs `mdx_q` | 8-bit quantization | **+0.08 dB** | -0.12 to +0.26 | 4/6 |
| `mdx_extra` vs `mdx_extra_q` | 8-bit quantization | **-0.17 dB** | -0.64 to -0.00 | 0/6 |
| `htdemucs` vs `htdemucs_ft` | per-source fine-tuning at roughly 4x compute | **+0.25 dB** | -0.40 to +0.61 | 5/6 |

The first two rows are the leak estimate, and the win column is the part that matters: the contaminated checkpoint does not merely average higher, it wins on every single track. A genuinely better architecture would trade wins across material of this diversity (metal, Tamil film, Irish folk, blues). A model that never loses on the exact songs it trained on is showing recall, not skill. On unseen material that advantage should shrink or vanish.

The quantization rows are the incidental finding: 8-bit costs 0.2 dB or less, so quantized checkpoints are essentially free quality-wise and the smaller download is worth having.

### Does this explain the original 9.02 dB result on Schoolboy Facination?

Yes. On that track `mdx_extra_q` scores 9.12 dB here, reproducing the 9.02 dB that was reported. But `mdx_q`, the same architecture and the same quantization trained WITHOUT the test set, scores 6.42 dB on the identical excerpt. The entire +2.70 dB advantage sits with the checkpoint that trained on this song.

Against `htdemucs_ft` at 7.05 dB, the original comparison read as "a cheap 2021 model beats a 2023 model at four times the compute". Once the contaminated checkpoint is set aside, `mdx_q` at 6.42 dB is -0.63 dB against `htdemucs_ft`, which is the ordinary ordering everyone expected. The surprise was train-on-test leakage, not a model discovery, and it should not be repeated as a finding.

## What this run did not measure

- Full tracks. Every score is one 60s vocal-dense excerpt per song, chosen for maximum vocal energy, so quiet intros and instrumental passages are excluded and absolute dB values run optimistic.
- Human listening. SI-SDR, SIR, SAR and LSD are all proxies, and no one has listened to these stems yet. Listening clips have NOT been generated for this run; run `shootout_clips.py` to produce them.
- Knob sensitivity. overlap is pinned at 0.25 and shifts at 0 for every model. The earlier ladder found under 0.2 dB across the whole overlap range, which is why they are pinned, but no model was tuned individually.
- Real DJ library material. MUSDB18-HQ is mostly live-band recordings. Electronic material with heavy sidechain and reverb-drenched vocals is under-represented, so transfer to another corpus requires evidence.
- Any model outside demucs. No comparison against MDX23, BS-RoFormer, or the commercial separators.
- Six of the twelve planned tracks. The fixed sample set in `shootout_spec.py` is 12 tracks; the retained run contains 6 completed entries. Hip-hop, pop rock, metal, Tamil film, Irish folk and blues are represented; produced pop, electronic, industrial rock and soul are not. The six that ran were the first six of a list fixed before any score existed, so they are a truncation rather than a selected subset.

## Per-track windows

| track | genre | window | vocal rms dBFS | silent hops |
| --- | --- | ---: | ---: | ---: |
| Al James - Schoolboy Facination | hip-hop, rapped male lead | 139.0s to 199.0s | -22.2 | 0.8% |
| Zeno - Signs | pop rock, sung male lead | 109.0s to 169.0s | -22.5 | 14.2% |
| Timboz - Pony | metal, screamed male over dense guitars | 59.0s to 119.0s | -27.9 | 7.5% |
| Sambasevam Shanmugam - Kaathaadi | Tamil film, non-Western instrumentation | 106.0s to 166.0s | -20.0 | 0.0% |
| Enda Reilly - Cur An Long Ag Seol | Irish-Gaelic folk, sparse acoustic | 92.0s to 152.0s | -22.2 | 20.0% |
| Cristina Vane - So Easy | blues, female over slide guitar | 184.0s to 244.0s | -23.2 | 7.5% |

Windows are chosen on TRUE vocal energy, never on a separator's guess, and the identical window is cut from the mixture and the truth so the pair is sample-aligned.

Generated by `scripts/bench/shootout_report.py` from `model_shootout.json`.

-Claude
