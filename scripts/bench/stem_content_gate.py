# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy<2", "soundfile"]
# ///
"""CONTENT GATE: reject a listening-set candidate whose stems have nothing in them.

The last four-stem round picked its track by LOWEST mixture baseline, on the
theory that the hardest track is the most informative one. Nobody checked
whether each individual stem actually CONTAINED anything, and the winner's BASS
stem turned out to be near-empty: six arms were all rated on sixty seconds of
almost nothing, which is not a measurement, it is a waste of a listening
session. This runs BEFORE any clip is encoded and refuses a track on evidence.

Five numbers per stem, per candidate window:
  - rms_dbfs        loudness of the whole window, dBFS (0 = full scale)
  - active_frac     fraction of 50 ms frames above ACTIVE_FLOOR_DBFS -- catches a
                    stem that is loud for two bars and silent for the rest, which
                    an RMS average alone hides
  - energy_share    this stem's energy over the sum of all four stems' energy.
                    Denominator is the STEM SUM, not the mixture: stems cancel
                    against each other in the mixture, so mixture energy is not a
                    meaningful whole to take a share of.
  - rms_below_loudest_db   how far this stem sits under the loudest stem in the
                    SAME window
  - peak_below_loudest_db  how far this stem's peak sits under the loudest peak

The last two are the pair that actually caught the complaint, and the reason the
first three alone were not enough. The rejected Timboz bass stem measured -30 dBFS
RMS with 98% of its frames "active", so on absolute bars it looked fine; what made
it unratable is that it sat 8.9 dB under the loudest stem and its PEAK never came
within 10.4 dB of the loudest peak. Set the system volume so the other three stems
are comfortable and that stem is not audibly there. Absolute level is the wrong
question; level RELATIVE to what shares the window is the right one.

A stem failing ANY bar is a stem that cannot be rated, so the track is rejected
outright. Rejections are printed with the offending number, so the choice is
auditable rather than asserted.

Window choice is part of the gate: a track is scanned at WINDOW_HOP_S steps and
keeps its BEST window (the one whose weakest stem is strongest), because
rejecting a track for a badly chosen window would be the same error in reverse.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 per-stem RMS, activity and energy share for every candidate window
    [if] a stem file is missing [then ⛔️] RuntimeError naming it, no silent skip
    [if] stems differ in sample rate or length [then ⛔️] RuntimeError
  ✔︎ ✅ 🎯 an auditable pass/fail table with the reason on every failed row
    [if] every candidate fails [then ⛔️] exit 1 and say so, do not pick a loser
  ✔︎ ✅ 🎯 a JSON verdict naming the winner and the window to cut
    [if] the winner's weakest stem is below the bar [then ⛔️] RuntimeError

Run:
  uv run scripts/bench/stem_content_gate.py \
    --tracks-dir .tmp/bench/musdb4/tracks --json-out scripts/bench/stem_content_gate.json

-Claude
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

#----- config -----------------------------------------------------------------

STEMS: tuple[str, ...] = ("vocals", "drums", "bass", "other")
WINDOW_S: float = 60.0
WINDOW_HOP_S: float = 10.0
# Skip the first and last few seconds: MUSDB tracks routinely open on a count-in
# and end on a fade, and neither is representative material to rate.
EDGE_TRIM_S: float = 5.0
FRAME_S: float = 0.050

# A frame quieter than this is not something a listener can rate. -50 dBFS is
# roughly 60 dB below a mastered mix, i.e. inaudible next to the other stems.
ACTIVE_FLOOR_DBFS: float = -50.0

# Rejection bars. Each is the level below which the stem stops being ratable.
MIN_RMS_DBFS: float = -40.0     # quieter than this over a whole minute is empty
MIN_ACTIVE_FRAC: float = 0.35   # present for at least a third of the window
MIN_ENERGY_SHARE: float = 0.03  # 3% of total stem energy, below that it is noise
# Relative bars, calibrated on the stem that was rejected as "virtually nothing in
# it": it measured -8.9 dB RMS and -10.4 dB peak against the loudest stem sharing
# its window. 8 dB is the line either side of that, and it also clears every
# candidate whose four stems are genuinely all in the room.
MIN_RMS_BELOW_LOUDEST_DB: float = -8.0
MIN_PEAK_BELOW_LOUDEST_DB: float = -8.0

_EPS: float = 1e-12


#----- measurement ------------------------------------------------------------

@dataclass(frozen=True)
class StemContent:
    """What one stem actually contains inside one window."""
    stem: str
    rms_dbfs: float
    peak_dbfs: float
    active_frac: float
    energy_share: float
    rms_below_loudest_db: float
    peak_below_loudest_db: float
    spectral_centroid_hz: float

    def failures(self) -> list[str]:
        out: list[str] = []
        if self.rms_dbfs < MIN_RMS_DBFS:
            out.append(f"near-silent ({self.rms_dbfs:.1f} dBFS < {MIN_RMS_DBFS:.0f})")
        if self.active_frac < MIN_ACTIVE_FRAC:
            out.append(f"mostly gaps ({self.active_frac * 100:.0f}% active "
                       f"< {MIN_ACTIVE_FRAC * 100:.0f}%)")
        if self.energy_share < MIN_ENERGY_SHARE:
            out.append(f"trivial share ({self.energy_share * 100:.1f}% of stem energy "
                       f"< {MIN_ENERGY_SHARE * 100:.0f}%)")
        if self.rms_below_loudest_db < MIN_RMS_BELOW_LOUDEST_DB:
            out.append(f"buried ({self.rms_below_loudest_db:.1f} dB under the loudest stem "
                       f"< {MIN_RMS_BELOW_LOUDEST_DB:.0f})")
        if self.peak_below_loudest_db < MIN_PEAK_BELOW_LOUDEST_DB:
            out.append(f"never surfaces (peak {self.peak_below_loudest_db:.1f} dB under the "
                       f"loudest peak < {MIN_PEAK_BELOW_LOUDEST_DB:.0f})")
        return out


def _dbfs(value: float) -> float:
    return 20.0 * float(np.log10(max(value, _EPS)))


def _mono(block: np.ndarray) -> np.ndarray:
    return block if block.ndim == 1 else block.mean(axis=1)


def _rms(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(block)))) if block.size else 0.0


def _active_frac(block: np.ndarray, sample_rate: int) -> float:
    frame = max(1, round(FRAME_S * sample_rate))
    usable = (block.size // frame) * frame
    if usable == 0:
        raise RuntimeError("window shorter than one analysis frame")
    frames = block[:usable].reshape(-1, frame)
    frame_rms = np.sqrt(np.mean(np.square(frames), axis=1))
    floor = 10.0 ** (ACTIVE_FLOOR_DBFS / 20.0)
    return float(np.mean(frame_rms >= floor))


def _spectral_centroid_hz(block: np.ndarray, sample_rate: int) -> float:
    """Energy-weighted mean frequency. Four stems whose centroids are far apart
    means the track spans a broad range rather than four takes on the same band,
    which is what makes it a demanding separation test."""
    spectrum = np.abs(np.fft.rfft(block * np.hanning(block.size)))
    freqs = np.fft.rfftfreq(block.size, d=1.0 / sample_rate)
    weight = float(np.sum(spectrum))
    return float(np.sum(freqs * spectrum) / weight) if weight > _EPS else 0.0


def _read_window(path: Path, start_sample: int, length: int) -> tuple[np.ndarray, int]:
    if not path.is_file():
        raise RuntimeError(f"stem file missing: {path}")
    block, sample_rate = sf.read(path, start=start_sample, frames=length,
                                 dtype="float32", always_2d=True)
    if block.shape[0] != length:
        raise RuntimeError(
            f"{path} yielded {block.shape[0]} of {length} requested samples")
    return block, int(sample_rate)


def _si_sdr_db(estimate: np.ndarray, target: np.ndarray) -> float:
    """Scale-invariant SDR. Used here only for the MIXTURE against each true stem:
    the do-nothing floor, i.e. how hard that stem is to pull out at all."""
    target_energy = float(np.dot(target, target))
    if target_energy <= _EPS:
        raise RuntimeError("si-sdr against a silent target is undefined")
    scale = float(np.dot(estimate, target)) / target_energy
    projection = scale * target
    noise = estimate - projection
    return 10.0 * float(np.log10(
        max(float(np.dot(projection, projection)), _EPS) / max(float(np.dot(noise, noise)), _EPS)))


def measure_window(track_dir: Path, start_sample: int,
                   length: int) -> tuple[list[StemContent], dict[str, float]]:
    """Content stats for all four stems over one window, plus the do-nothing floor."""
    mono: dict[str, np.ndarray] = {}
    rate: int | None = None
    for stem in ("mixture",) + STEMS:
        block, sample_rate = _read_window(track_dir / f"{stem}.wav", start_sample, length)
        if rate is None:
            rate = sample_rate
        elif sample_rate != rate:
            raise RuntimeError(
                f"{track_dir.name}: {stem} is {sample_rate} Hz, expected {rate} Hz -- "
                "refusing to resample a reference stem")
        mono[stem] = _mono(block)
    assert rate is not None

    energies = {s: float(np.sum(np.square(mono[s]))) for s in STEMS}
    total = sum(energies.values())
    if total <= _EPS:
        raise RuntimeError(f"{track_dir.name}: all four stems are silent in this window")

    rms_db = {s: _dbfs(_rms(mono[s])) for s in STEMS}
    peak_db = {s: _dbfs(float(np.max(np.abs(mono[s])))) for s in STEMS}
    loudest_rms = max(rms_db.values())
    loudest_peak = max(peak_db.values())

    floors = {s: round(_si_sdr_db(mono["mixture"], mono[s]), 3) for s in STEMS}

    return [
        StemContent(
            stem=stem,
            rms_dbfs=round(rms_db[stem], 2),
            peak_dbfs=round(peak_db[stem], 2),
            active_frac=round(_active_frac(mono[stem], rate), 4),
            energy_share=round(energies[stem] / total, 4),
            rms_below_loudest_db=round(rms_db[stem] - loudest_rms, 2),
            peak_below_loudest_db=round(peak_db[stem] - loudest_peak, 2),
            spectral_centroid_hz=round(_spectral_centroid_hz(mono[stem], rate), 1),
        )
        for stem in STEMS
    ], floors


#----- window search ----------------------------------------------------------

@dataclass
class WindowVerdict:
    track: str
    start_s: float
    length_s: float
    stems: list[StemContent]
    mixture_floor_si_sdr: dict[str, float]

    @property
    def failures(self) -> dict[str, list[str]]:
        return {c.stem: c.failures() for c in self.stems if c.failures()}

    @property
    def passed(self) -> bool:
        return len(self.failures) == 0

    @property
    def weakest_share(self) -> float:
        """The gate's headroom: how present the LEAST present stem is."""
        return min(c.energy_share for c in self.stems)

    @property
    def weakest_active(self) -> float:
        return min(c.active_frac for c in self.stems)

    @property
    def balance_db(self) -> float:
        """Worst level gap in the window, RMS or peak, whichever buries a stem
        further. Closer to 0 means all four stems are equally in the room."""
        return min(min(c.rms_below_loudest_db, c.peak_below_loudest_db) for c in self.stems)

    @property
    def spectral_span_octaves(self) -> float:
        """Distance between the lowest and highest stem centroid, in octaves.
        This is the 'broader range' term: a wide span means the four stems really
        occupy different parts of the spectrum."""
        centroids = [c.spectral_centroid_hz for c in self.stems if c.spectral_centroid_hz > 0]
        if len(centroids) < 2:
            raise RuntimeError(f"{self.track}: fewer than two stems have any spectral content")
        return round(float(np.log2(max(centroids) / min(centroids))), 2)

    @property
    def hardness_db(self) -> float:
        """Mean do-nothing floor across the four stems, negated so higher is
        harder. A track the mixture already resembles is an easy test."""
        return round(-float(np.mean([f for f in self.mixture_floor_si_sdr.values()])), 2)


def _track_frames(track_dir: Path) -> tuple[int, int]:
    info = sf.info(str(track_dir / "mixture.wav"))
    return int(info.frames), int(info.samplerate)


def best_window(track_dir: Path) -> tuple[WindowVerdict, list[WindowVerdict]]:
    """Scan the track and return its best window plus every window examined.

    'Best' is the window whose WEAKEST stem is strongest. Picking by average
    would hand the prize back to a track carried by three loud stems and one
    empty one, which is the exact failure this gate exists to catch.
    """
    frames, rate = _track_frames(track_dir)
    length = round(WINDOW_S * rate)
    trim = round(EDGE_TRIM_S * rate)
    hop = round(WINDOW_HOP_S * rate)
    last_start = frames - trim - length
    if last_start < 0:
        raise RuntimeError(
            f"{track_dir.name} is {frames / rate:.1f}s, too short for a "
            f"{WINDOW_S:.0f}s window with {EDGE_TRIM_S:.0f}s edge trim")

    examined: list[WindowVerdict] = []
    for start in range(trim, last_start + 1, hop):
        stems, floors = measure_window(track_dir, start, length)
        examined.append(WindowVerdict(
            track=track_dir.name,
            start_s=round(start / rate, 2),
            length_s=WINDOW_S,
            stems=stems,
            mixture_floor_si_sdr=floors,
        ))
    if not examined:
        raise RuntimeError(f"{track_dir.name}: no candidate window fitted the track")

    passing = [w for w in examined if w.passed]
    pool = passing if passing else examined
    best = max(pool, key=lambda w: (w.balance_db, w.weakest_active))
    return best, examined


#----- reporting --------------------------------------------------------------

def _rule(width: int = 104) -> str:
    return "+" + "-" * (width - 2) + "+"


def print_table(verdicts: list[WindowVerdict]) -> None:
    header = (f"| {'track / window':38} | {'stem':6} | {'RMS dBFS':>9} | {'peak':>7} | "
              f"{'active':>7} | {'share':>6} | {'vs loud':>8} | {'pk vs':>7} | "
              f"{'cntr Hz':>8} | {'floor':>7} | {'verdict':7} |")
    print(_rule(len(header)))
    print(header)
    print(_rule(len(header)))
    for v in verdicts:
        for i, c in enumerate(v.stems):
            mark = "FAIL" if c.failures() else "ok"
            name = f"{v.track[:31]} @{v.start_s:.0f}s" if i == 0 else ""
            print(f"| {name:38} | {c.stem:6} | {c.rms_dbfs:9.2f} | {c.peak_dbfs:7.2f} | "
                  f"{c.active_frac * 100:6.1f}% | {c.energy_share * 100:5.1f}% | "
                  f"{c.rms_below_loudest_db:8.1f} | {c.peak_below_loudest_db:7.1f} | "
                  f"{c.spectral_centroid_hz:8.0f} | "
                  f"{v.mixture_floor_si_sdr[c.stem]:7.1f} | {mark:7} |")
        for stem, reasons in v.failures.items():
            print(f"| {'':38} | -> {stem} REJECTED: {'; '.join(reasons)}")
        print(f"| {'':38} | balance {v.balance_db:.1f} dB, spectral span "
              f"{v.spectral_span_octaves:.2f} oct, hardness {v.hardness_db:.1f} dB")
        print(_rule(len(header)))


#----- main -------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tracks-dir", required=True, type=Path,
                    help="dir of <Artist - Title>/{mixture,vocals,drums,bass,other}.wav")
    ap.add_argument("--json-out", required=True, type=Path)
    args = ap.parse_args()

    if not args.tracks_dir.is_dir():
        raise RuntimeError(f"tracks dir does not exist: {args.tracks_dir}")
    track_dirs = sorted(p for p in args.tracks_dir.iterdir() if p.is_dir())
    if len(track_dirs) < 5:
        raise RuntimeError(
            f"only {len(track_dirs)} candidates in {args.tracks_dir}; the whole point of "
            "this gate is a parallel sanity check, so it needs at least 5")

    bests: list[WindowVerdict] = []
    scanned: dict[str, int] = {}
    for track_dir in track_dirs:
        best, examined = best_window(track_dir)
        bests.append(best)
        scanned[track_dir.name] = len(examined)

    print_table(bests)

    passing = [v for v in bests if v.passed]
    print(f"\n[INFO] {len(passing)} of {len(bests)} candidates pass the content gate. Bars: "
          f"RMS >= {MIN_RMS_DBFS:.0f} dBFS, active >= {MIN_ACTIVE_FRAC * 100:.0f}%, "
          f"share >= {MIN_ENERGY_SHARE * 100:.0f}%, RMS and peak both within "
          f"{-MIN_RMS_BELOW_LOUDEST_DB:.0f} dB of the loudest stem. Each row is that track's "
          "BEST window, so a rejection means no window in the track works.")
    for v in bests:
        if v.passed:
            print(f"  [PASS] {v.track} @{v.start_s:.0f}s -- balance {v.balance_db:.1f} dB, "
                  f"span {v.spectral_span_octaves:.2f} oct, hardness {v.hardness_db:.1f} dB")
        else:
            why = "; ".join(f"{s}: {', '.join(r)}" for s, r in v.failures.items())
            print(f"  [FAIL] {v.track} @{v.start_s:.0f}s -- {why}")

    if not passing:
        print("\n[ERROR] no MUSDB candidate passes. Do NOT fall back to the least-bad one: "
              "use one of the maintainer's own tracks with a frozen best-arm pseudo-reference instead.")
        sys.exit(1)

    # Two things were asked for and they pull against each other: every stem
    # genuinely present (balance) and a demanding example with broad range
    # (span x hardness). Rank on the product of the two after the gate, and print
    # the runners-up, so the trade is visible rather than buried in a sort key.
    def rank_key(v: WindowVerdict) -> float:
        return round(v.spectral_span_octaves * v.hardness_db + v.balance_db, 3)

    ordered = sorted(passing, key=rank_key, reverse=True)
    print("\n[INFO] passing candidates ranked by span x hardness + balance:")
    for v in ordered:
        print(f"  {rank_key(v):7.2f}  {v.track} @{v.start_s:.0f}s "
              f"(span {v.spectral_span_octaves:.2f} oct, hardness {v.hardness_db:.1f} dB, "
              f"balance {v.balance_db:.1f} dB)")
    winner = ordered[0]
    print(f"\n[OK] winner: {winner.track} @{winner.start_s:.0f}s for {winner.length_s:.0f}s "
          f"-- weakest stem holds {winner.weakest_share * 100:.1f}% of stem energy and sits "
          f"only {winner.balance_db:.1f} dB under the loudest")

    args.json_out.write_text(json.dumps({
        "gate": {
            "window_s": WINDOW_S,
            "window_hop_s": WINDOW_HOP_S,
            "edge_trim_s": EDGE_TRIM_S,
            "frame_s": FRAME_S,
            "active_floor_dbfs": ACTIVE_FLOOR_DBFS,
            "min_rms_dbfs": MIN_RMS_DBFS,
            "min_active_frac": MIN_ACTIVE_FRAC,
            "min_energy_share": MIN_ENERGY_SHARE,
            "min_rms_below_loudest_db": MIN_RMS_BELOW_LOUDEST_DB,
            "min_peak_below_loudest_db": MIN_PEAK_BELOW_LOUDEST_DB,
            "energy_share_denominator": "sum of the four stems' energy, not the mixture",
        },
        "windows_scanned": scanned,
        "candidates": [
            {
                "track": v.track,
                "start_s": v.start_s,
                "length_s": v.length_s,
                "passed": v.passed,
                "failures": v.failures,
                "weakest_energy_share": v.weakest_share,
                "balance_db": v.balance_db,
                "spectral_span_octaves": v.spectral_span_octaves,
                "hardness_db": v.hardness_db,
                "mixture_floor_si_sdr": v.mixture_floor_si_sdr,
                "stems": [asdict(c) for c in v.stems],
            }
            for v in bests
        ],
        "winner": {
            "track": winner.track,
            "start_s": winner.start_s,
            "length_s": winner.length_s,
            "weakest_energy_share": winner.weakest_share,
            "balance_db": winner.balance_db,
            "spectral_span_octaves": winner.spectral_span_octaves,
            "hardness_db": winner.hardness_db,
            "mixture_floor_si_sdr": winner.mixture_floor_si_sdr,
        },
    }, indent=2) + "\n", encoding="utf-8")
    print(f"[OK] wrote {args.json_out}")


if __name__ == "__main__":
    main()
