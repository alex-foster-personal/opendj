"""The key lane's producer semver, and the one place it is written down.

`apps/analysis/record.py` makes `producer_version` a required field of every own
record and `apps/analysis/lanes.py` ranks the canonical pointer by it, so this
lane needs a version an ordering function can read. Bump the PATCH for a fix
that cannot change any emitted key, the MINOR for a policy change that can (a
new threshold, a new profile, a new chroma front end), and the MAJOR for a
change to the lane payload shape.

WHAT 1.0.0 IS. The classical profile DSP: `librosa.feature.chroma_cqt` (the
same call `scripts/keybench/run_krumhansl.py` and the shipped `librosa`
backend already make, 36 bins per octave, automatic tuning) over
Krumhansl-Kessler 1982 profiles, our own `apps/analysis_key.canon`
canonicalizer, and `no_tonal_center`. It is model-FREE: no third-party weights
of any kind, so `uses_model=False` and `model_sha256=None` on every record it
writes.

The upgraded shipping producer `specs/native-analysis-v1.md` section 5 names --
24 key templates fitted on an AGREE calibration bucket, with an HPCP-style
front end -- is rung (a)/(b) of the classical upgrade and is NOT here: its
fitting procedure is `nav1-key-r0`'s deliverable and is tracked in issue #1601.
When it lands, it changes the values this producer emits, which is a MINOR bump
and re-queues every track (spec section 3, "a producer version bump re-queues
affected lanes").

-Claude
"""
from __future__ import annotations

from typing import Literal

#: 1.1.0 (Fri 2 Oct 2026): the no_tonal_center margin threshold moved from the
#: 0.02 placeholder to the round-1 measured 0.005 (`flags.py`), which changes
#: what this producer publishes, so every key record re-queues.
PRODUCER_VERSION = "1.1.0"

#: The producer half of the `own_<lane>.<producer>` backend name. The record
#: contract parses the backend name and checks it against the record body, so
#: this is the value `AnalysisRecord.producer` carries too.
PRODUCER: Literal["backfill"] = "backfill"

#: Selection lane this package produces for: `key` covers the scalar key and
#: the key-change segments together (spec section 3, "Selection lanes").
LANE: Literal["key"] = "key"

__all__ = ["LANE", "PRODUCER", "PRODUCER_VERSION"]
