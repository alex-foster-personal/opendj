"""The lane's producer semver, and the one place it is written down.

`apps/analysis/record.py` makes `producer_version` a required field of every
own record and `apps/analysis/lanes.py` ranks the canonical pointer by it, so
this lane needs a version an ordering function can read. `beat_this_runner.py`
already carried one, but that file is a PEP 723 script that imports torch at
module scope: the repo venv cannot import it, so a record writer running in the
repo venv could not read the constant it has to stamp.

Rather than let the two drift, the runner keeps its own literal (it is the
thing that produced the beats and its payload states which version made them)
and `tests/analysis_beatgrid/test_version.py` reads that literal out of the
file as TEXT and asserts it equals this one. A record writer additionally
compares the version stamped in the payload it is about to convert against
this constant and refuses a mismatch, so a payload produced by a different
runner cannot be written under this version's name.

Bump the PATCH for a fix that cannot change any emitted beat, the MINOR for a
policy change that can, and the MAJOR for a change to the lane payload shape.
Whatever the bump, change it in BOTH files: the test is what makes that
mandatory rather than remembered.

-Claude
"""
from __future__ import annotations

from typing import Literal

#: Semver for the whole `apps/analysis_beatgrid` producer: the model runner,
#: the octave policy, the changepoint detector, the bar-phase assignment and
#: the lane payload builder together. 1.1.0 is the version
#: `beat_this_runner.py` shipped in PR #1514 and the round-1 measurements were
#: taken at; the record-writing half added here changes no emitted beat.
#: 1.3.0 (NATIVE-17) changes the octave policy only: the model's own level wins
#: a two-octave tie, a genre tempo family can pick the octave, and line mode
#: folds half-time sections. The runner's beats are unchanged from 1.2.0.
#: 1.4.0 (NATIVE-19, the Preview line) changed the served grid only: the lane's
#: grid fit defaults to `const_regions` (one constant-tempo line where the track
#: holds one tempo) instead of `raw`.
#: 1.5.0 (NAE-22, main) changed the served offset only, +15 ms to -10 ms, because
#: rekordbox positions moved onto our MP3-lead-in-trimmed timeline
#: (`grid_design` docstring).
#: 1.6.0 is the union of both, from the Preview integration (PR #4974): a record
#: written at 1.4.0 lacks the new offset and one at 1.5.0 lacks the new default
#: fit, so both re-queue. The runner's beats are unchanged throughout.
PRODUCER_VERSION = "1.6.0"

#: The producer half of the `own_<lane>.<producer>` backend name. The record
#: contract parses the backend name and checks it against the record body, so
#: this is the value `AnalysisRecord.producer` carries too. Typed as its own
#: single-value Literal (rather than importing `apps.analysis.lane_enums.
#: Producer`) so this package keeps zero intra-repo imports, per the module
#: docstring above; a narrower Literal is still assignable everywhere the
#: wider `Producer` type is expected.
PRODUCER: Literal["backfill"] = "backfill"

#: Selection lane this package produces for.
LANE: Literal["beatgrid"] = "beatgrid"

__all__ = ["LANE", "PRODUCER", "PRODUCER_VERSION"]
