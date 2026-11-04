"""The QLT letter grade and its hover text (QLTSCAN-02).

Two inputs only, as `(private path)` decides: what the
bitrate and container say (`audio_quality.py`, no decode) and what the
spectrum shows (`audio_scan.py`, one decode). Issues other than corruption
never move the letter; they are a dot on the badge and a line in the hover,
because a letter that blends six things cannot be explained in a hover.

    A  Safe on any system          lossless, or 256 kbps effective or more
    B  Good on a club system       192-255 kbps
    C  Thin on a big rig           128-191 kbps
    D  Rough, replace if you can   under 128 kbps, decode errors, or over 1 s truncated

The spectrum can only LOWER a letter, and only when the scan found a
brick-wall edge (a lossy encoder's signature). A gentle roll-off, which is
what a dark master has, never caps anything. Cut-off caps, in Hz:

    under 15,000 -> D   (the earlier thread's 96 kbps encode read 15.4 kHz)
    under 17,000 -> C   (128 kbps encodes read 16.7-16.8 kHz: they must be C)
    under 19,000 -> B

The B/C line was 16.5 kHz in the doc. It moved to 17.0 kHz on Tue 27 Oct
2026 because a 128 kbps LAME encode measures 16.8 kHz with this scanner, and
the doc's intent is that a 128k-sourced file grades C. To reverse, set
`GradeThresholds.c_below_hz` back to 16500.

CALIBRATION, Tue 27 Oct 2026, a read-only sample of 200 files from the maintainer's
library (every 15th row of the Mon 26 Oct library scan), scanned on silver:
cut-offs agree with the earlier thread's (median difference 28 Hz, p90 209
Hz). Genuine 128 kbps MP3s read 12.4-16.7 kHz (median 16.1), so a 16.5 kHz
B/C line would let some into B; 17.0 keeps all of them in C. Old-encoder
128 kbps files read 15.2-15.4 kHz, the same as a 96 kbps source, so the D
line stays at 15 kHz: the spectrum cannot tell those apart, and calling a
genuine 128k file D would be the bad signal the maintainer's rule forbids. Letters on
the sample: A 31, B 115, C 51, D 3 (bitrate alone said A 130). Most of the
drop is "256 kbps" files whose content cuts off at 17-19 kHz (median 17.2).

Before a scan has run (or after a rule or file change made the stored facts
stale) the letter comes from bitrate alone and `checked` is False: the badge
draws it hollow, and the hover says "not checked yet". It never reads as
verified. Pure functions: no I/O, so fixtures drive them directly.

MINI-PRD
--------
  ✔︎ ✅ 🎯 grade(): one letter from the venue rung and, when present, the facts.
    [if] a FLAC brick-walls at 16.8 kHz [then] C ⛔️ A because the container is lossless
    [if] a lossless master rolls off gently [then] A ⛔️ capped by its edge
    [if] a 320 kbps mp3 has decode errors [then] D ⛔️ A
    [if] the file was never scanned [then] the bitrate letter, checked False
         ⛔️ a letter that reads as verified
  ✔︎ ✅ 🎯 hover_lines(): first line is the letter and meaning; unmeasured
    lines say "not checked", never "good".

-Claude
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Literal

Letter = Literal["A", "B", "C", "D"]

MEANINGS: dict[str, str] = {
    "A": "Safe on any system",
    "B": "Good on a club system",
    "C": "Thin on a big rig",
    "D": "Rough, replace if you can",
}

#: Venue rung (audio_quality.VENUES key) -> letter from bitrate and container.
LETTER_BY_VENUE: dict[str, Letter] = {
    "stadium": "A",
    "warehouse": "A",
    "club": "A",
    "house_party": "B",
    "lounge": "C",
    "naughty_step": "D",
}

#: Issues that force D (the file is damaged, not merely thin).
CORRUPTION_ISSUES: frozenset[str] = frozenset({"decode_errors", "truncated"})
#: Keys a stored facts record must carry for its letter to be trusted.
REQUIRED_FACT_KEYS: tuple[str, ...] = ("issues", "cutoff_hz", "brick_wall")


@dataclass(frozen=True)
class GradeThresholds:
    """The cut-off caps. A change here changes `grade_rule_version()`."""

    d_below_hz: int = 15000
    c_below_hz: int = 17000  # was 16500 in the decision doc, see module docstring
    b_below_hz: int = 19000


THRESHOLDS = GradeThresholds()
_ORDER = "ABCD"


def grade_rule_version(t: GradeThresholds = THRESHOLDS) -> str:
    digest = hashlib.sha256(json.dumps(asdict(t), sort_keys=True).encode()).hexdigest()
    return f"grade-1:{digest[:12]}"


@dataclass(frozen=True)
class Grade:
    letter: Letter | None
    #: True only when stored facts for the current file and rule exist.
    checked: bool
    meaning: str
    issues: tuple[str, ...]
    hover: tuple[str, ...]
    cutoff_hz: int | None

    def row_dict(self) -> dict[str, Any]:
        """The compact block a listing row carries. The hover is not on the
        row: it would push each row past its byte budget, so the badge loads
        it on hover from `GET /api/v1/audio-quality/{stable_id}`."""
        return {"letter": self.letter, "checked": self.checked, "issues": list(self.issues)}

    def as_dict(self) -> dict[str, Any]:
        return {
            "letter": self.letter,
            "checked": self.checked,
            "meaning": self.meaning,
            "issues": list(self.issues),
            "hover": "\n".join(self.hover),
            "cutoff_hz": self.cutoff_hz,
        }


# ----- letter ----------------------------------------------------------------
def _worse(a: Letter, b: Letter) -> Letter:
    return a if _ORDER.index(a) >= _ORDER.index(b) else b


def spectral_cap(cutoff_hz: int | None, brick_wall: bool, t: GradeThresholds = THRESHOLDS) -> Letter | None:
    """The letter the spectrum caps at, or None when it caps nothing."""
    if not brick_wall or cutoff_hz is None:
        return None
    if cutoff_hz < t.d_below_hz:
        return "D"
    if cutoff_hz < t.c_below_hz:
        return "C"
    if cutoff_hz < t.b_below_hz:
        return "B"
    return None


def letter_for(venue: str | None, facts: dict[str, Any] | None, t: GradeThresholds = THRESHOLDS) -> Letter | None:
    if venue is None:
        return None
    letter = LETTER_BY_VENUE[venue]
    if facts is None:
        return letter
    if CORRUPTION_ISSUES & set(facts["issues"]):
        return "D"
    cap = spectral_cap(facts["cutoff_hz"], bool(facts["brick_wall"]), t)
    return _worse(letter, cap) if cap is not None else letter


# ----- hover -----------------------------------------------------------------
def _khz(hz: float) -> str:
    return f"{hz / 1000:.1f} kHz"


def _rate(hz: int) -> str:
    return f"{hz / 1000:g} kHz"


def _channels(n: int) -> str:
    return {1: "mono", 2: "stereo"}.get(n, f"{n} channels")


def _likely_source(cutoff_hz: int, t: GradeThresholds) -> str:
    if cutoff_hz < t.d_below_hz:
        return "96 kbps or lower"
    if cutoff_hz < t.c_below_hz:
        return "128 kbps"
    return "about 192 kbps"


_ISSUE_TEXT: dict[str, Callable[[dict[str, Any]], str]] = {
    "decode_errors": lambda f: f"{f['decode_errors']} decode errors",
    "truncated": lambda f: f"audio stops {f['truncated_s']:.0f} s before the header's length",
    "clipping": lambda f: f"Clipping: {f['clip_runs']} flat-topped runs, worst {f['clip_longest_ms']:.1f} ms",
    "dc_offset": lambda f: f"DC offset: {f['dc_offset'] * 100:.1f}% of full scale",
    "padded_24bit": lambda _f: "Padded 24-bit: low bits always zero, so this is 16-bit audio",
    "upsampled": lambda f: f"Upsampled: {_rate(f['sample_rate'])} file with nothing above {_khz(f['cutoff_hz'])}",
    "mono_in_stereo": lambda _f: "Mono content in a stereo file",
    "silence_at_start": lambda f: f"{f['lead_silence_s']:.1f} s of silence at the start",
    "silence_at_end": lambda f: f"{f['trail_silence_s']:.1f} s of silence at the end",
}


def issue_text(key: str, facts: dict[str, Any]) -> str:
    """One hover phrase per issue key, each with its number."""
    if key not in _ISSUE_TEXT:
        raise ValueError(f"unhandled issue key: {key!r}")
    return _ISSUE_TEXT[key](facts)


def _bandwidth_line(facts: dict[str, Any], lossless: bool, t: GradeThresholds) -> str:
    cutoff = facts.get("cutoff_hz")
    if cutoff is None:
        return "Bandwidth: not measurable (too little audio above the noise floor)"
    if not facts.get("brick_wall"):
        return f"Bandwidth: no encoder cut-off found (energy reaches {_khz(cutoff)})"
    line = f"Bandwidth: cuts off at {_khz(cutoff)}"
    if cutoff >= t.b_below_hz:
        return line
    source = _likely_source(cutoff, t)
    if lossless:
        return f"{line}. Lossless files reach about 20 kHz, so this was likely made from a {source} MP3"
    return f"{line}, so the source was {source} or lower"


def _head_line(letter: Letter, facts: dict[str, Any] | None, lossless: bool) -> str:
    if facts is not None:
        return f"{letter} -- {MEANINGS[letter]}"
    what = "lossless container, contents not verified" if lossless else "bitrate only, contents not verified"
    return f"{letter} (not checked yet) -- {what}"


def _format_lines(fmt: str, facts: dict[str, Any] | None, lossless: bool) -> list[str]:
    container_note = " (lossless container)" if lossless else ""
    if facts is None:
        return [f"Format: {fmt}{container_note}", "Sample rate: not checked"]
    rate = _rate(facts["sample_rate"])
    if not lossless:
        rate_line = f"Sample rate: {rate} (lossy, no fixed bit depth)"
    elif facts.get("bit_depth"):
        rate_line = f"Sample rate / bit depth: {rate} / {facts['bit_depth']}-bit"
    else:
        rate_line = f"Sample rate: {rate} (bit depth not in the header)"
    return [f"Format: {fmt}, {_channels(facts['channels'])}{container_note}", rate_line]


def _bitrate_line(kbps: int | None, facts: dict[str, Any] | None, lossless: bool) -> str:
    if kbps is None:
        return "Bitrate: not measurable"
    claimed = facts.get("claimed_kbps") if facts else None
    said = f" (header says {round(claimed)})" if claimed and not lossless else ""
    return f"Bitrate: {kbps} kbps effective{said}"


def _issues_line(facts: dict[str, Any]) -> str:
    issues = list(facts["issues"])
    if not issues:
        return "Issues: none found (clipping, DC offset, decode errors checked)"
    return "Issues: " + "; ".join(issue_text(key, facts) for key in issues)


def hover_lines(
    quality: dict[str, Any],
    facts: dict[str, Any] | None,
    scan_error: str | None,
    letter: Letter | None,
    t: GradeThresholds = THRESHOLDS,
) -> tuple[str, ...]:
    """The hover, one line each: letter, format, rate, bitrate, bandwidth, issues."""
    if letter is None:
        return (f"Quality unknown -- {quality.get('blurb', '')}",)
    lossless = bool(quality.get("lossless"))
    fmt = str(quality.get("container") or "").lstrip(".").upper() or "unknown format"
    lines = [_head_line(letter, facts, lossless), *_format_lines(fmt, facts, lossless)]
    lines.append(_bitrate_line(quality.get("kbps"), facts, lossless))
    if facts is None:
        lines.append("Bandwidth: not checked")
        lines.append(f"Issues: scan failed -- {scan_error}" if scan_error else "Issues: not checked")
    else:
        lines.append(_bandwidth_line(facts, lossless, t))
        lines.append(_issues_line(facts))
    return tuple(lines)


# ----- grade -----------------------------------------------------------------
def grade(
    quality: dict[str, Any],
    facts: dict[str, Any] | None,
    scan_error: str | None = None,
    t: GradeThresholds = THRESHOLDS,
) -> Grade:
    """The badge for one row. `quality` is `audio_quality.Quality.as_dict()`;
    `facts` is a current `ScanFacts.as_dict()` or None (never scanned, stale,
    or the scan failed, in which case `scan_error` says why). A facts record
    missing a REQUIRED_FACT_KEYS entry is never trusted: the row is unchecked
    and the hover names the missing keys, so it reads as a failed scan."""
    if facts is not None:
        missing = [key for key in REQUIRED_FACT_KEYS if key not in facts]
        if missing:
            facts, scan_error = (
                None,
                f"stored scan facts are incomplete (missing {', '.join(missing)}); a re-scan is needed",
            )
    letter = letter_for(quality.get("venue"), facts, t)
    issues = tuple(facts["issues"]) if facts else ()
    return Grade(
        letter=letter,
        checked=facts is not None and letter is not None,
        meaning=MEANINGS[letter] if letter else str(quality.get("blurb", "")),
        issues=issues,
        hover=hover_lines(quality, facts, scan_error, letter, t),
        cutoff_hz=facts.get("cutoff_hz") if facts else None,
    )


__all__ = [
    "CORRUPTION_ISSUES",
    "LETTER_BY_VENUE",
    "MEANINGS",
    "REQUIRED_FACT_KEYS",
    "THRESHOLDS",
    "Grade",
    "GradeThresholds",
    "grade",
    "grade_rule_version",
    "hover_lines",
    "issue_text",
    "letter_for",
    "spectral_cap",
]
