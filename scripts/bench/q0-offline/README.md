# Q0: offline region-detector analyses

Reproducible, GPU-free, network-free, spend-gate-free analyses of the vocal region detector,
written Fri 24 Jul 2026. They read only `data/state/stems/` and `data/state/vocal-cache/`, and
reconstruct the mix by summing the four stems of the 30 existing bundles.

Preserved here because they were written in an ephemeral session scratchpad and are worth
re-running. The harness was validated before its conclusions were trusted: it reproduces shipped
coverage to a median of +0.2 points across 21 tracks, so it measures the pipeline rather than
itself.

| Script | What it answers |
|---|---|
| `q0_free.py` | Recomputes the vocals/mix ratio envelope offline from existing stem bundles |
| `q0_intro_and_fix.py` | Tests the intro-blowup mechanism and simulates an absolute vocals_rms floor across six values |
| `q0_leak_test.py` | Correlation of the vocals stem against drums+bass+other inside detected regions |
| `leak.json` | Output of the leak test |

## What they established

- The intro-blowup mechanism is FALSIFIED. Tracks whose first region starts at 0.00s have intros
  8.0 dB below their own median; tracks starting later, 7.6 dB. A -28 dBFS intro measured a ratio
  of 0.005, which is 20x BELOW the 0.10 entry threshold, not double it. On genuinely quiet intros
  the separated stem is also near-zero, so the ratio COLLAPSES rather than blowing up.
- High-coverage entries are NOT accompaniment leakage. Vocals-versus-backing correlation inside
  detected regions is 0.045 to 0.051 on high-coverage tracks, LOWER than the 0.060 to 0.081 of
  low-coverage controls, while carrying 34-66% of accompaniment RMS. That is what a present vocal
  looks like.
- A proposed absolute-floor fix is INERT: at -50 dBFS mean coverage moves 1.1 points and 1 of 21
  tracks moves more than 5 points; at -60 dBFS, 0 of 21. You cannot filter loud content with a
  quiet-content threshold.

## The lesson worth keeping

This whole question was settled for free, offline, in about 20 minutes, after several rounds of
agents arguing about it from source-code reasoning. When a dispute outlasts the price of settling
it, that is the signal to stop arguing and measure.
