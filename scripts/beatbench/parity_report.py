"""Measure a GPU beat run against a CPU run of the same clips.

WHY THIS IS ITS OWN MODULE. It used to live in `scripts/modal_beat_farm.py`,
which imports `modal` at module scope for its decorators, so the comparator
could not be imported without `modal` installed and had no tests at all. It
also could not be RUN the way its own documentation said: `python
scripts/modal_beat_farm.py compare` puts `scripts/` on `sys.path` rather than
the repository root, so the absolute import of the alignment rules raised
`ModuleNotFoundError: No module named 'scripts'` before the comparator started
(Codex P1 BLOCKING, PR #1660, discussion_r3975140340). Nothing here imports
`modal`, and it runs as `python -m scripts.beatbench.parity_report`.

WHAT A PARITY CLAIM REQUIRES. Equal beat times are not enough. Two runs that
agree while using different checkpoints, thresholds or frame rates have not
shown that the accelerator is irrelevant, they have shown that these
particular clips were insensitive to the difference. So the analyzer identity
is checked BEFORE any row is compared, and a missing field is a refusal rather
than a match against None.

USAGE
    python -m scripts.beatbench.parity_report --gpu gpu.json --cpu cpu.json

WHAT IS HELD EQUAL AND WHAT IS NOT. `ANALYZER_IDENTITY_KEYS` must MATCH:
weights, checkpoint, threshold, frame rate, and the analyzer's own source
digest. `ENVIRONMENT_KEYS` must be PRESENT and are expected to differ, because
they are what the claim is made across. `torch_version` is one of them, and it
genuinely differs in the committed evidence (2.14.0+cpu against 2.5.1): the CPU
wheel index and the CUDA image do not ship a common release, so requiring
equality would make the measurement impossible rather than rigorous. Every
differing environment field is PRINTED on every run, so the scope of the claim
is read off the output instead of being assumed.

EXIT
    0  every paired clip agreed, on identical decoded audio
    1  a real difference: analyzer identity, fingerprints, beats, or key sets
    2  UNMEASURED: not enough overlap to say anything. Never read as a pass.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics as st
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.beatbench.parity import align, beat_gap_ms

DEFAULT_TOLERANCE_MS = 10.0

#: What must be IDENTICAL for a parity claim to mean anything. These describe
#: the analyzer, not the machine it ran on.
ANALYZER_IDENTITY_KEYS = (
    "beat_this_version",
    "checkpoint",
    "model_sha256",
    "threshold",
    "fps",
    # The analyzer's own CODE, digested from the bytes that ran. The claimed
    # experiment is "the same code on two accelerators", and nothing here
    # checked the code: a CPU artifact from a different revision of
    # `apps/analysis_beatgrid/beat_this_runner.py` passed whenever this
    # 100-clip subset happened to emit the same beats, which is exactly the
    # case where a real difference is invisible (Codex P1 BLOCKING, PR #1660).
    # `scripts/modal_beat_farm.py` bakes that file into its image and digests
    # it, and the runner digests itself, so the two agree byte for byte.
    "runner_sha256",
)

#: What must be PRESENT but is expected to DIFFER: the whole point of the
#: comparison is that the beats survive these changing. Requiring them present
#: is still not decoration -- an artifact that cannot say which device produced
#: it cannot be shown to be the GPU side of a CPU/GPU comparison at all.
#:
#: `numpy_version` and `soundfile_version` are here rather than above for a
#: reason worth stating: the GPU image and the nucbox CPU environment resolve
#: their own wheels, so requiring them EQUAL would refuse every real parity
#: run. Requiring them PRESENT is what makes a decode divergence attributable
#: instead of mysterious.
ENVIRONMENT_KEYS = (
    "producer", "producer_version", "torch_version", "device",
    # `torchaudio_version` belongs here rather than in the identity keys for
    # the same reason `torch_version` does, and it matters more: it is the
    # DECODER. `beat_this.inference.load_audio` calls `torchaudio.load` first
    # and only drops to soundfile on an exception, so torchaudio produced every
    # sample behind every `decode_fingerprint` this comparator holds equal. The
    # two sides genuinely run 2.11.0 against 2.5.1, so requiring equality would
    # make the measurement impossible; printing it makes the fact that the
    # fingerprints match ACROSS a decoder version difference part of the
    # result instead of an unrecorded coincidence (Codex P1 BLOCKING, PR #1660,
    # discussion_r3977265421).
    "numpy_version", "torchaudio_version", "soundfile_version",
)

#: The one required field whose null is MEANINGFUL: a CPU producer has no
#: accelerator to name. It is checked by `_role_refusal` instead of by value,
#: because what matters about it is which SIDE carries it.
ROLE_KEY = "gpu"

#: Per-ROW decode identity, as opposed to the per-artifact header keys above.
#: `decode_fingerprint` hashes sample bytes only, so these are the fields that
#: can differ while it matches.
DECODE_IDENTITY_FIELDS = ("sample_rate", "n_frames")


def finite_tolerance_ms(raw: str) -> float:
    """A tolerance that can actually reject something.

    `argparse` accepts `nan` and `inf` as floats, and every `gap > tolerance`
    comparison against them is false, so `--tolerance-ms nan` turned a 200 ms
    divergence into `0 clips exceed nan ms` and exit 0: malformed input
    rendered as passing parity evidence (Codex P2 BLOCKING, PR #1660).
    """
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError(
            f"tolerance must be a finite, non-negative number of milliseconds, "
            f"not {raw!r}. A non-finite bound cannot be exceeded, so every "
            "comparison against it passes and the report becomes evidence of "
            "nothing."
        )
    return value


def load_payload(path: str) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """`(results, header)` from a runner artifact.

    Results are NOT re-keyed to a basename. Collapsing `a/01.mp3` and
    `b/01.mp3` onto `01.mp3` would silently drop one side of a real comparison
    and could make two runs over DIFFERENT tracks look like matching key sets.
    If two producers disagree about how they name a clip, the key-set check in
    `compare` is where that must surface, loudly.

    The header used to be discarded here, which is what let a comparison run
    across two different analyzer configurations (Codex P1 BLOCKING, PR #1660,
    discussion_r3975279142).
    """
    payload = json.loads(Path(path).read_text())
    header = {k: v for k, v in payload.items() if k != "results"}
    return dict(payload.get("results", {})), header


def _role_refusal(gpu: dict[str, Any], cpu: dict[str, Any]) -> str | None:
    """Whether these two artifacts are actually an accelerator and a CPU.

    Equality of the analyzer fields says the two sides ran the same ANALYZER.
    Nothing said they ran on different COMPUTE, so passing the committed CPU
    artifact as both `--gpu` and `--cpu` exited 0 and reported CPU/GPU parity
    from one machine (Codex P1 BLOCKING, PR #1660, discussion_r3976348796).

    Stated as an INVARIANT rather than a value: the accelerator side must NAME
    an accelerator and the CPU side must name none. Pinning `device == "cuda"`
    would have to be maintained forever and would refuse the first ROCm or MPS
    run for no reason connected to the claim.
    """
    problems = []
    if not gpu.get(ROLE_KEY):
        problems.append(
            f"the --gpu side names no accelerator ({ROLE_KEY}={gpu.get(ROLE_KEY)!r})"
        )
    if str(gpu.get("device", "")).lower() == "cpu":
        problems.append("the --gpu side reports device 'cpu'")
    if cpu.get(ROLE_KEY) is not None:
        problems.append(
            f"the --cpu side names an accelerator ({ROLE_KEY}={cpu.get(ROLE_KEY)!r})"
        )
    if str(cpu.get("device", "")).lower() != "cpu":
        problems.append(
            f"the --cpu side reports device {cpu.get('device')!r}, not 'cpu'"
        )
    if not problems:
        return None
    return (
        "these two artifacts are not an accelerator run and a CPU run, so equal "
        "beats would say nothing about the accelerator: " + "; ".join(problems)
    )


def _identity_refusal(gpu: dict[str, Any], cpu: dict[str, Any]) -> str | None:
    """Why these two artifacts may not be compared, or None if they may be."""
    required = (*ANALYZER_IDENTITY_KEYS, *ENVIRONMENT_KEYS, ROLE_KEY)
    absent = [
        f"{side}:{key}"
        for side, header in (("gpu", gpu), ("cpu", cpu))
        for key in required
        if key not in header
    ]
    if absent:
        return (
            "an artifact that does not say what produced it cannot be shown to "
            "have run the same analyzer as the other side, and reading a missing "
            f"field as None makes every such artifact compare equal: {absent}"
        )
    # PRESENT is not the same as KNOWN. `resolve_checkpoint_sha256` returns None
    # by design when it cannot find the weights that were loaded, and two nulls
    # compare equal, so a membership-only check accepted two artifacts with
    # UNKNOWN weights or code and called them identical (Codex P1 BLOCKING, PR
    # #1660, discussion_r3976348788). `gpu` is excluded because its null is the
    # meaningful one; `_role_refusal` is what checks it.
    valueless = [
        f"{side}:{key}"
        for side, header in (("gpu", gpu), ("cpu", cpu))
        for key in (*ANALYZER_IDENTITY_KEYS, *ENVIRONMENT_KEYS)
        if header[key] is None or header[key] == ""
    ]
    if valueless:
        return (
            "an artifact that records a required identity with no value has not "
            "answered the question, and two unknowns compare equal, so this would "
            f"report parity between two runs of unknown provenance: {valueless}"
        )
    role = _role_refusal(gpu, cpu)
    if role is not None:
        return role
    differing = {
        key: {"gpu": gpu[key], "cpu": cpu[key]}
        for key in ANALYZER_IDENTITY_KEYS
        if gpu[key] != cpu[key]
    }
    if differing:
        return (
            "the two sides ran DIFFERENT analyzer configurations, so equal beats "
            "would not show that the accelerator is irrelevant:\n"
            + json.dumps(differing, indent=2)
        )
    return None


@dataclass
class PairMeasurements:
    """Everything `_measure_pairs` found, named rather than positional.

    It returned a 4-tuple until downbeats had to be measured too, at which
    point the call site was unpacking six positional lists and the next field
    would have been unreadable.
    """

    fingerprint_mismatch: list[str]
    fingerprint_absent: list[str]
    decode_field_mismatch: list[str]
    gaps: list[tuple[float, str]]
    unmeasurable: list[str]
    downbeat_gaps: list[tuple[float, str]]
    downbeat_presence: list[str]


def _measure_pairs(
    gpu: dict[str, dict[str, Any]],
    cpu: dict[str, dict[str, Any]],
    mapping: dict[str, str],
) -> PairMeasurements:
    """Fingerprint, beat and DOWNBEAT agreement for every mapped pair.

    Absence is tracked SEPARATELY from mismatch. Two absent fingerprints used
    to compare equal and print `OK decode_fingerprint`, so the hard
    decoding-parity control reported a pass in exactly the case where it had
    not run at all (Codex P1 BLOCKING, PR #1660, discussion_r3975279150).

    DOWNBEATS ARE PART OF THE CLAIM, and this read only `beats`. The runner
    emits a downbeat sequence, the product uses it for bar phase and for
    synchronized controls, and two runs agreeing on every beat while disagreeing
    on where the bar starts are not the same analysis. Moving one committed
    downbeat by a whole SECOND left the documented parity command reporting a
    clean pass (Codex P1 BLOCKING, PR #1660, discussion_r3977660365).

    An empty downbeat list on BOTH sides is agreement, not an unmeasurable
    pair: the analyzer is entitled to find no bar. On ONE side it is a real
    divergence, and `beat_gap_ms` cannot say so -- it returns None for an empty
    input, which the beats path reads as unmeasurable -- so presence is checked
    before distance rather than through it.
    """
    m = PairMeasurements([], [], [], [], [], [], [])
    for key, cpu_key in sorted(mapping.items()):
        g, c = gpu[key], cpu[cpu_key]
        if g.get("error") or c.get("error"):
            m.unmeasurable.append(key)
            continue
        gp, cp = g.get("decode_fingerprint"), c.get("decode_fingerprint")
        if not gp or not cp:
            m.fingerprint_absent.append(key)
        elif gp != cp:
            m.fingerprint_mismatch.append(key)
        # The fingerprint hashes the SAMPLE BYTES and nothing else, so two
        # decodes that produce identical floats at different rates hash the
        # same while being materially different inputs -- the model is handed
        # `sample_rate` separately, and every beat time is frame index over
        # fps. Changing one committed artifact's rate from 22050 to 48000 left
        # this command printing `OK decode_fingerprint` and exiting 0 (Codex P1
        # BLOCKING, PR #1660, discussion_r3978351339). `n_frames` is checked
        # beside it for the same reason and by the same argument the class
        # question asks: it is the other decode-identity integer the rows
        # already carry and nothing compared. Both are absolute equalities;
        # neither has a legitimate reason to differ between two runs of one
        # analyzer over one file.
        # Absent on BOTH sides is `None == None`, which read as OK decode
        # (Codex P1 BLOCKING, discussion_r3982585235), so each side must carry
        # a positive int before the values are compared at all.
        m.decode_field_mismatch.extend(
            f"{key} ({field} {g.get(field)!r} vs {c.get(field)!r})"
            for field in DECODE_IDENTITY_FIELDS
            if not (_positive_int(g.get(field)) and g.get(field) == c.get(field))
        )
        gap = beat_gap_ms(g.get("beats") or [], c.get("beats") or [])
        if gap is None:
            m.unmeasurable.append(key)
        else:
            m.gaps.append((gap, key))
        g_down, c_down = g.get("downbeats") or [], c.get("downbeats") or []
        if bool(g_down) != bool(c_down):
            m.downbeat_presence.append(key)
        elif g_down:
            down_gap = beat_gap_ms(g_down, c_down)
            if down_gap is None:
                raise AssertionError(f"{key}: non-empty downbeats measured as None")
            m.downbeat_gaps.append((down_gap, key))
    return m


def _positive_int(value: object) -> bool:
    """A real decode count: an int, not a bool, above zero."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _report_downbeats(
    measured: PairMeasurements, tolerance_ms: float
) -> list[tuple[float, str]]:
    """Print the downbeat verdict, return the clips over tolerance.

    Same tolerance as the beats: a downbeat IS a beat, marked. Reported on its
    own lines rather than folded into the beat count, so a run that agrees on
    the pulse and disagrees on the bar says exactly that. Lifted out of
    `compare` because adding it took that function over the mccabe limit the
    quality ratchet gates on.
    """
    down_over = [(g, n) for g, n in measured.downbeat_gaps if g > tolerance_ms]
    if measured.downbeat_presence:
        print(
            f"[compare] FAIL downbeats: {len(measured.downbeat_presence)} clips have "
            f"downbeats on one side and none on the other, e.g. "
            f"{measured.downbeat_presence[:5]}",
            file=sys.stderr,
        )
    if down_over:
        print(
            f"[compare] FAIL downbeats: {len(down_over)} of "
            f"{len(measured.downbeat_gaps)} clips exceed {tolerance_ms:.0f} ms, "
            f"worst {down_over[:3]}. The two runs agree on the pulse and "
            "disagree on where the bar starts, which is not the same analysis.",
            file=sys.stderr,
        )
    elif measured.downbeat_gaps and not measured.downbeat_presence:
        # Guarded on `downbeat_presence` as well: printing OK over the 99 clips
        # that agreed, beside a FAIL naming the 100th, is a report that
        # contradicts itself. Found by running the presence control rather than
        # by reading the code.
        print(
            f"[compare] OK downbeats: median worst-gap "
            f"{st.median([g for g, _ in measured.downbeat_gaps]):.2f} ms across "
            f"{len(measured.downbeat_gaps)} clips"
        )
    return down_over


def compare(gpu_path: str, cpu_path: str, tolerance_ms: float) -> int:
    """Measure a GPU run against a CPU run of the same clips.

    EVERY shared row must be measurable and the key sets must match. A previous
    version scored only the intersection and could exit 0 on it, so a GPU
    artifact holding ONE successful clip out of a 100-clip CPU run reported
    parity if that clip matched, and an incomplete or half-failed run could
    become release evidence (Codex P1 BLOCKING, PR #1660). Incompleteness is
    now a failure, not a smaller denominator.
    """
    gpu, gpu_header = load_payload(gpu_path)
    cpu, cpu_header = load_payload(cpu_path)
    if not gpu or not cpu:
        print(
            f"[compare] UNMEASURED: {len(gpu)} rows on the GPU side, {len(cpu)} on "
            "the CPU side. This is a capability report, not a pass.",
            file=sys.stderr,
        )
        return 2
    refusal = _identity_refusal(gpu_header, cpu_header)
    if refusal is not None:
        print(f"[compare] FAIL analyzer provenance: {refusal}", file=sys.stderr)
        return 1
    # NAMED, never silently accepted. `torch_version` cannot be required EQUAL
    # here and that is a property of the world, not an oversight: the CPU wheel
    # index and the CUDA image do not ship a common release, so a strict rule
    # would make CPU/GPU parity unmeasurable rather than strict (Codex P1
    # BLOCKING, PR #1660, discussion_r3976483273). What CAN be done is refuse to
    # let the difference pass unread, so every run prints exactly which parts of
    # the environment differed and the reader can see the claim's real scope:
    # this is parity of the two DEPLOYMENTS, not of an isolated accelerator
    # variable.
    environment_differs = {
        key: {"gpu": gpu_header[key], "cpu": cpu_header[key]}
        for key in ENVIRONMENT_KEYS
        if gpu_header[key] != cpu_header[key]
    }
    print(
        "[compare] environment differs, which is what parity is claimed ACROSS: "
        + json.dumps(environment_differs, sort_keys=True)
    )
    mapping = align(gpu, cpu)
    if mapping is None or len(mapping) != len(cpu):
        print(
            f"[compare] FAIL key sets: {len(gpu)} GPU rows against {len(cpu)} CPU "
            "rows do not correspond one to one. A parity claim over a subset is "
            "not a parity claim; compare runs over the same corpus.",
            file=sys.stderr,
        )
        return 1

    measured = _measure_pairs(gpu, cpu, mapping)
    fingerprint_mismatch = measured.fingerprint_mismatch
    fingerprint_absent = measured.fingerprint_absent
    gaps, unmeasurable = measured.gaps, measured.unmeasurable

    print(f"[compare] {len(mapping)} paired clips, {len(unmeasurable)} unmeasurable")
    if fingerprint_absent:
        print(
            f"[compare] UNMEASURED decode_fingerprint: {len(fingerprint_absent)} "
            f"clips carry none on one or both sides, e.g. {fingerprint_absent[:5]}. "
            "The decoding control did not run; it did not pass.",
            file=sys.stderr,
        )
    if fingerprint_mismatch:
        print(
            f"[compare] FAIL decode_fingerprint: {len(fingerprint_mismatch)} clips "
            f"differ, e.g. {fingerprint_mismatch[:5]}. Decoding is deterministic, "
            "so this is a real divergence and every beat figure below is "
            "measured on different audio.",
            file=sys.stderr,
        )
    if measured.decode_field_mismatch:
        print(
            f"[compare] FAIL decode identity: "
            f"{len(measured.decode_field_mismatch)} clips differ on "
            f"{' or '.join(DECODE_IDENTITY_FIELDS)}, e.g. "
            f"{measured.decode_field_mismatch[:3]}. The fingerprint hashes "
            "sample bytes only, so identical floats at a different rate hash "
            "equal while being a different input.",
            file=sys.stderr,
        )
    if not (fingerprint_absent or fingerprint_mismatch or measured.decode_field_mismatch):
        print(
            f"[compare] OK decode: fingerprint, "
            f"{' and '.join(DECODE_IDENTITY_FIELDS)} identical on all "
            f"{len(gaps)} clips"
        )

    if unmeasurable:
        # A row that errored or emitted nothing on either side is not evidence
        # of agreement, and dropping it would quietly shrink the denominator of
        # a claim that is about completeness as much as accuracy.
        print(
            f"[compare] FAIL {len(unmeasurable)} of {len(mapping)} paired clips are "
            f"unmeasurable (error or no beats on one side), e.g. {unmeasurable[:5]}",
            file=sys.stderr,
        )

    if not gaps:
        print("[compare] UNMEASURED: no clip had beats on both sides", file=sys.stderr)
        return 2
    gaps.sort(reverse=True)
    over = [(g, n) for g, n in gaps if g > tolerance_ms]
    values = [g for g, _ in gaps]
    print(
        f"[compare] beat times: median worst-gap {st.median(values):.2f} ms, "
        f"max {values[0]:.2f} ms on {gaps[0][1]}"
    )
    print(
        f"[compare] {len(over)} of {len(gaps)} clips exceed {tolerance_ms:.0f} ms"
        + (f", worst {over[:3]}" if over else "")
    )

    down_over = _report_downbeats(measured, tolerance_ms)

    failed = (
        fingerprint_mismatch or fingerprint_absent or over or unmeasurable
        or down_over or measured.downbeat_presence or measured.decode_field_mismatch
    )
    if failed:
        return 1
    if not measured.downbeat_gaps:
        # Neither side emitted a downbeat anywhere, so bar phase was never
        # compared. That is a legitimate analyzer answer and it is NOT a passed
        # control, so it exits 2 (UNMEASURED, never read as a pass) rather than
        # 0. This branch printed the warning and returned 0 when first written,
        # which is precisely the defect .claude/rules/verification.md names:
        # a failed measurement rendered as a result.
        print(
            "[compare] UNMEASURED downbeats: no clip carried downbeats on either "
            "side, so bar phase was never compared. Beats agreed; that is a "
            "narrower claim than this tool otherwise reports.",
            file=sys.stderr,
        )
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="compare a GPU run against a CPU run")
    ap.add_argument("--gpu", required=True, help="the accelerator side's artifact")
    ap.add_argument("--cpu", required=True, help="the CPU side's artifact")
    ap.add_argument("--tolerance-ms", type=finite_tolerance_ms,
                    default=DEFAULT_TOLERANCE_MS)
    args = ap.parse_args(argv)
    return compare(args.gpu, args.cpu, args.tolerance_ms)


if __name__ == "__main__":
    raise SystemExit(main())
