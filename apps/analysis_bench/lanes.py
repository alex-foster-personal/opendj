"""What a lane IS, so `--lane` never has to be interpreted twice.

Four v1 lanes share one harness. Everything that differs between them -- which
experiment log the round lands in, where that lane's counter is up to, which
scorer holds its ruler, which candidates stand as its controls -- is declared
here once, and the CLI reads it rather than branching on the lane name.

ALL FOUR V1 LANES HAVE A SCORER. `require_scorer`'s None branch and FOLLOW_UP
stay in place so a fifth lane can still ship registered-but-unscored and refuse
by name; they just no longer fire for loudness or waveform.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

# The counter each lane's rounds continue. Beatgrid's rounds 0 and 1 are already
# published (specs/beat-mapping-bench.md, PR #1514), so its next round is 2; the
# other three lanes have posted nothing through this harness and start at 0.
BEATGRID_ROUND_FLOOR = 2

DEFAULT_LOG = "specs/native-analysis-v1.md"

# Issue that owns the lanes still missing a scorer, quoted in the refusal so a
# reader is sent somewhere real rather than told "not implemented".
FOLLOW_UP = "#1477"

_CONTROLS = "apps.analysis_bench.controls"


class LaneError(RuntimeError):
    """A lane, or something a lane declares, was asked for and does not exist."""


@dataclass(frozen=True)
class Candidate:
    """One arm of a round: a command that turns a bundle into a results JSON."""

    name: str
    role: str  # "candidate" | "positive_control" | "negative_control"
    argv: tuple[str, ...]  # `{fixtures}` and `{out}` are substituted at run time
    note: str

    def command(self, *, fixtures: str, out: str) -> list[str]:
        return [
            part.format(fixtures=fixtures, out=out, python=sys.executable)
            for part in self.argv
        ]


@dataclass(frozen=True)
class Lane:
    name: str
    log_path: str
    round_floor: int
    scorer_module: str | None
    fixture_builder: str | None
    candidates: tuple[Candidate, ...]
    truth: str


def _control(lane: str, kind: str, name: str, note: str) -> Candidate:
    return Candidate(
        name=name,
        role=f"{kind}_control",
        argv=(
            "{python}", "-m", _CONTROLS,
            "--lane", lane, "--kind", kind,
            "--fixtures", "{fixtures}", "--out", "{out}",
        ),
        note=note,
    )


LANES: dict[str, Lane] = {
    "beatgrid": Lane(
        name="beatgrid",
        log_path=DEFAULT_LOG,
        round_floor=BEATGRID_ROUND_FLOOR,
        scorer_module="apps.analysis_bench.scorers.beatgrid_lane",
        fixture_builder="scripts/beatbench/fixtures.py (needs this Mac's rekordbox ANLZ)",
        truth="rekordbox ANLZ PQTZ beats inside each excerpt window",
        candidates=(
            Candidate(
                name="constant_128",
                role="negative_control",
                argv=("{python}", "scripts/beatbench/run_constant_bpm.py",
                      "--fixtures", "{fixtures}", "--out", "{out}"),
                note="rigid 128 BPM grid from t=0; the audio is never opened",
            ),
            _control("beatgrid", "positive",
                     "truth_offset_25ms",
                     "the reference grid shifted by a constant +25 ms"),
            Candidate(
                name="beat_this",
                role="candidate",
                argv=("uv", "run", "--no-project", "--script",
                      "scripts/beatbench/run_beat_this.py",
                      "--fixtures", "{fixtures}", "--out", "{out}"),
                note="Beat This! 1.1.0, CPU torch via PEP 723",
            ),
            Candidate(
                name="librosa",
                role="candidate",
                argv=("uv", "run", "--no-project", "--script",
                      "scripts/beatbench/run_librosa.py",
                      "--fixtures", "{fixtures}", "--out", "{out}"),
                note="librosa beat_track, the DSP baseline",
            ),
        ),
    ),
    "key": Lane(
        name="key",
        log_path=DEFAULT_LOG,
        round_floor=0,
        scorer_module="apps.analysis_bench.scorers.key_lane",
        fixture_builder="apps.analysis_bench fixtures build --lane key",
        truth="rekordbox djmdKey plus Mixed In Key, canonicalized to (pitch class, mode)",
        candidates=(
            _control("key", "negative", "constant_key",
                     "always answers C major (Camelot 8B), spec section 5's cited floor"),
            _control("key", "most_common", "most_common_key",
                     "always answers the most common rekordbox key in the bundle"),
            _control("key", "positive", "truth_echo", "answers the rekordbox reference key"),
            _control("key", "positive_mik", "truth_echo_mik", "answers the MIK reference key"),
        ),
    ),
    "waveform": Lane(
        name="waveform",
        log_path=DEFAULT_LOG,
        round_floor=0,
        scorer_module="apps.analysis_bench.scorers.waveform_lane",
        fixture_builder="scripts/build_waveform_bundle.py (needs this Mac's rekordbox PWV)",
        truth="rekordbox PWV3/PWV5/PWV6 per-band columns",
        candidates=(
            _control("waveform", "negative", "constant_band", "one flat band level everywhere"),
            _control("waveform", "positive", "truth_echo", "answers the reference columns"),
        ),
    ),
    "loudness": Lane(
        name="loudness",
        log_path=DEFAULT_LOG,
        round_floor=0,
        scorer_module="apps.analysis_bench.scorers.loudness_lane",
        fixture_builder="apps.analysis_bench fixtures build --lane loudness",
        truth="pyloudnorm LUFS and a 4x-oversampled true-peak reference",
        candidates=(
            _control("loudness", "negative", "constant_lufs", "always answers -14 LUFS"),
            _control("loudness", "positive", "truth_echo", "answers the reference LUFS"),
        ),
    ),
}


def get_lane(name: str) -> Lane:
    try:
        return LANES[name]
    except KeyError:
        known = ", ".join(sorted(LANES))
        raise LaneError(f"unknown lane {name!r}; known lanes are {known}") from None


def get_candidate(lane: Lane, name: str) -> Candidate:
    for candidate in lane.candidates:
        if candidate.name == name:
            return candidate
    known = ", ".join(c.name for c in lane.candidates)
    raise LaneError(f"lane {lane.name} has no candidate {name!r}; it declares {known}")


def controls(lane: Lane) -> tuple[Candidate, ...]:
    return tuple(c for c in lane.candidates if c.role.endswith("_control"))


def require_scorer(lane: Lane) -> str:
    """The lane's scorer module, or a refusal naming the issue that owes it one."""
    if lane.scorer_module is None:
        raise LaneError(
            f"lane {lane.name} has no scorer yet, so it cannot be run or scored. "
            f"The harness, bundle store and round log are shared and ready; the "
            f"{lane.name} scorer is the follow-up slice of {FOLLOW_UP}."
        )
    return lane.scorer_module
