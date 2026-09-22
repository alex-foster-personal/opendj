"""Stem durable-codec sizing vs the source file.

For lossy input, writing PCM WAV or a high bitrate for each stem can make
the bundle larger than its source. This module governs encoded size; it
does not make a quality claim or include the private source-corpus census.

Policy for LOSSY durable stems (opus):
  * target total bitrate across the four parts ~= source effective bitrate
  * each encoded part should land <= source file bytes (target, not a hard
    gate -- see "Size-vs-source is a KPI/TARGET" below)
  * FLAC/WAV control arms are exempt from the size check (eval / SI-SDR only)

Policy for the RoFormer 2-stem spike (flac/mp3), source format decides the
output codec -- no local post-pass ever needed, the runner emits the right
format itself, in-container:
  * source is lossless (flac/wav/aif/aiff) -> FLAC at the source bit depth
  * source is lossy (mp3/m4a) -> MP3, bitrate <= source bitrate: LAME 320
    CBR when the source itself is >= 320 kbps (320 CBR then literally cannot
    exceed it), else LAME V0 VBR (LAME's own ~245 kbps documented average)
  * an unrecognised source extension is a policy gap, not a guess: raise
  * the mp3 check above reuses the same "part bytes <= source bytes" target
    as opus (mp3 lives in LOSSY_SIZE_GATED too)

Two-rung MP3 encode ladder (closes the V0/near-320 size-ceiling gap):
  LAME V0 VBR has NO bitrate ceiling -- it targets a quality level, not a
  cap, so a separated stem (an isolated instrumental especially, which can
  be harder to encode than the original mix) can land ABOVE the V0 rung's
  own ~245 kbps average, and therefore above the source file's bytes for a
  source that only just missed the 320 CBR branch. The native 320 CBR branch is not immune either: a source
  whose own probed kbps is only fractionally above 320 can still be
  fractionally beaten by its own 320 CBR stem once container overhead is
  counted. Both are the SAME failure shape -- rung 1 has no hard ceiling
  relative to THIS source -- so both get the same fix:
    * rung 1 is unchanged (mp3_lame_settings_for_source_kbps, as above)
    * if rung 1's encoded bytes for a stem exceed the source file's bytes
      (lossy_stem_exceeds_source), re-encode that ONE stem once more, at
      the nearest standard CBR <= floor(source_kbps) from MP3_CBR_LADDER
      (mp3_rung2_cbr_kbps) -- a CBR encode at or under the source's own
      average kbps is guaranteed under source bytes for equal duration
    * no ladder rung fits under a source below MP3_CBR_LADDER's floor
      (192 kbps) -- that is a policy gap, not a guess: raise

Size-vs-source is a KPI/TARGET, not a hard gate (revised design, see
StemSizeViolation below): assert_lossy_stems_not_larger_than_source no
longer raises when a stem lands over the source's bytes after the ladder --
it RETURNS the violations instead, so the runner keeps the file, records
the overage, and moves on. This applies even at the terminal rung (rung 2):
if rung 2 still lands over source bytes, that is logged as a target miss,
not a hard failure. What DOES stay hard (unrelated to size, never touched
by this demotion): the codec must be the one the policy picked, the encoded
duration must match the source, and the file must decode cleanly -- those
are correctness invariants, not a target. A caller passing a non-positive
source_bytes/source_kbps is still a contract bug and still raises
(StemSizeError/ValueError) -- only a genuine size overage is downgraded
from raise to record.

WHAT THIS MODULE DOES NOT DECIDE: every rule above is denominated in
bytes, encoded part bytes against source file bytes. Nothing here ranks
codecs by decode time, GPU time or deck-load latency. Private corpus timing
and byte measurements are not included in the public source. A size-policy
decision must not be presented as a decode-speed acceptance result.

MINI-PRD
--------
  ✔︎ ✅ 🎯 opus kbps per stem is derived from source effective kbps
    [if] source is 320 kbps [then] each stem encodes at 80 kbps (320/4)
    [if] source is 128 kbps [then] floor at OPUS_MIN_KBPS (still usable)
    [if] source is 256 kbps [then] 64 kbps per stem

  ✔︎ ✅ 🎯 post-encode size CHECK for lossy durable parts (KPI/target, not a
    hard gate -- see the module docstring's revised design)
    [if] any stem part bytes > source bytes [then] it comes back as a
      StemSizeViolation in the returned list -- no raise, the file is kept
    [if] no part exceeds [then] an empty list, same as always
    [if] codec is flac/wav (control) [then] skip the check entirely (empty
      list, never evaluated as a violation)
    [if] source_bytes is non-positive [then ⛔️] raise StemSizeError -- a
      caller contract bug, not a size target miss

  ✔︎ ✅ 🎯 output codec decided from the source extension
    [if] source ext is .flac/.wav/.aif/.aiff [then] "flac"
    [if] source ext is .mp3/.m4a [then] "mp3"
    [if] source ext is anything else [then ⛔️] raise UnknownSourceFormatError

  ✔︎ ✅ 🎯 mp3 settings decided from the probed source bitrate
    [if] source_kbps >= MP3_CBR_KBPS [then] "lame_320kbps_cbr"
    [if] source_kbps < MP3_CBR_KBPS [then] "lame_v0_vbr"

  ✔︎ ✅ 🎯 rung-2 CBR ladder decision (pure; the runner does the re-encode)
    [if] a rung-1 stem part is <= source bytes [then] lossy_stem_exceeds_source
      is False, no rung 2 needed
    [if] a rung-1 stem part is > source bytes [then] lossy_stem_exceeds_source
      is True, and mp3_rung2_cbr_kbps returns the nearest standard CBR
      (320/256/224/192) <= floor(source_kbps)
    [if] source_kbps floors below MP3_CBR_LADDER's lowest rung (192)
      [then ⛔️] raise StemSizeError -- no rung fits, a policy gap not a guess

  ✔︎ ✅ 🎯 the lossy rung is chosen on bytes, independently of decode latency
    [if] source ext is .mp3 [then] the output codec stays "mp3"
    [if] a codec is chosen to buy decode time [then] verify encoded size
      separately; this module makes no decode-speed acceptance claim
    [if] someone cites a decode-speed claim to this module [then ⛔️] it is
      out of scope here: this module never ranked codecs by speed

  ✔︎ ✅ 🎯 per-part size ratio (for meta.json / status reporting)
    [if] part_bytes == source_bytes [then] lossy_stem_size_ratio returns 1.0
    [if] part_bytes is double source_bytes [then] it returns 2.0

-Claude
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# Four demucs parts. Keep in sync with stem_artifacts.STEM_PARTS.
STEM_PART_COUNT: int = 4
OPUS_MIN_KBPS: int = 48
OPUS_MAX_KBPS: int = 128  # never one stem alone near a 320k source
LOSSY_SIZE_GATED: frozenset[str] = frozenset({"opus", "mp3"})
CONTROL_CODECS: frozenset[str] = frozenset({"flac", "wav"})

# RoFormer 2-stem spike: source format -> output codec policy.
LOSSLESS_SOURCE_EXTS: frozenset[str] = frozenset({".flac", ".wav", ".aif", ".aiff"})
LOSSY_SOURCE_EXTS: frozenset[str] = frozenset({".mp3", ".m4a"})
MP3_CBR_KBPS: int = 320  # LAME 320 CBR rung; below this, LAME V0 VBR instead

# Rung-2 standard CBR ladder (descending). A source that cannot absorb even
# the lowest rung (192 kbps) has no fit -- mp3_rung2_cbr_kbps raises rather
# than guess a bitrate that was never validated against MP3_CBR_KBPS.
MP3_CBR_LADDER: tuple[int, ...] = (320, 256, 224, 192)


class StemSizeError(ValueError):
    """A durable-stem-policy CALLER contract is violated (non-positive size/
    duration/bitrate, or no ladder rung fits). Never raised for a stem that
    merely landed over source bytes -- see StemSizeViolation for that."""


class UnknownSourceFormatError(ValueError):
    """A source file extension has no output-format policy (fail fast, no guessing)."""


def effective_bitrate_kbps(size_bytes: int, duration_s: float) -> float:
    """Size-over-duration kbps (same honesty rule as apps.shared.audio_quality)."""
    if size_bytes <= 0:
        raise ValueError(f"size_bytes must be positive, got {size_bytes}")
    if duration_s <= 0:
        raise ValueError(f"duration_s must be positive, got {duration_s}")
    return size_bytes * 8 / duration_s / 1000


def opus_kbps_per_stem(source_kbps: float) -> int:
    """Per-stem Opus bitrate so four stems ~= source total bitrate.

    Cap and floor keep a single stem from matching a warehouse MP3 alone, and
    keep very low sources from becoming unusable for mute/solo listening.
    """
    if source_kbps <= 0:
        raise ValueError(f"source_kbps must be positive, got {source_kbps}")
    raw = round(source_kbps / STEM_PART_COUNT)
    return max(OPUS_MIN_KBPS, min(OPUS_MAX_KBPS, raw))


def opus_ffmpeg_audio_args(source_kbps: float) -> list[str]:
    """ffmpeg ``-c:a libopus -b:a …`` args for one stem at the policy bitrate."""
    kbps = opus_kbps_per_stem(source_kbps)
    return ["-c:a", "libopus", "-b:a", f"{kbps}k"]


def stem_output_codec_for_source_ext(source_ext: str) -> str:
    """"flac" for a lossless source, "mp3" for a lossy one.

    Fails fast on an unrecognised extension -- an unhandled source format is
    a policy gap to close, not something to guess through.
    """
    ext = source_ext.lower()
    if ext in LOSSLESS_SOURCE_EXTS:
        return "flac"
    if ext in LOSSY_SOURCE_EXTS:
        return "mp3"
    raise UnknownSourceFormatError(
        f"no output-format policy for source extension {source_ext!r}; known "
        f"lossless={sorted(LOSSLESS_SOURCE_EXTS)}, lossy={sorted(LOSSY_SOURCE_EXTS)}"
    )


def mp3_lame_settings_for_source_kbps(source_kbps: float) -> str:
    """"lame_320kbps_cbr" when the source itself can absorb 320 CBR, else
    "lame_v0_vbr" (LAME's own ~245 kbps documented average).

    This is the "whichever lands under source bitrate" half of the lossy-
    source stem policy. It only picks the starting rung -- the actual encoded
    bytes are still checked against the source file size by
    assert_lossy_stems_not_larger_than_source (codec="mp3"), so this function
    never itself guarantees the fit.
    """
    if source_kbps <= 0:
        raise ValueError(f"source_kbps must be positive, got {source_kbps}")
    return "lame_320kbps_cbr" if source_kbps >= MP3_CBR_KBPS else "lame_v0_vbr"


def lossy_stem_exceeds_source(*, source_bytes: int, part_bytes: int, codec: str) -> bool:
    """True when one lossy stem part alone is already larger than the source.

    Pure per-part check the two-rung ladder uses to decide whether THAT
    stem needs a rung-2 re-encode, before the batch size check
    (assert_lossy_stems_not_larger_than_source) runs across every part.
    Control codecs (flac/wav) are never gated, same as the batch check.
    """
    if codec in CONTROL_CODECS or codec not in LOSSY_SIZE_GATED:
        return False
    if source_bytes <= 0:
        raise StemSizeError(f"source_bytes must be positive, got {source_bytes}")
    return part_bytes > source_bytes


def mp3_rung2_cbr_kbps(source_kbps: float) -> int:
    """Rung 2 of the mp3 encode ladder: the nearest standard CBR from
    MP3_CBR_LADDER that is <= floor(source_kbps).

    Only called after rung 1 (mp3_lame_settings_for_source_kbps) already
    produced a stem part larger than the source file -- V0 VBR has no
    bitrate ceiling, and the native 320 CBR rung can fractionally exceed a
    source that is itself only fractionally above 320 kbps once container
    overhead is counted (see the module docstring). A CBR encode at or
    under the source's own average kbps is guaranteed under source bytes
    for equal duration: bitrate * duration <= source_kbps * duration ==
    source_bytes (up to rounding). The caller (the runner's in-container
    re-encode) still checks the result afterwards -- this function only
    picks the rung, it does not itself guarantee the encoded bytes land
    under it (and per the revised design, even a rung-2 miss is recorded,
    not raised -- see assert_lossy_stems_not_larger_than_source).
    """
    if source_kbps <= 0:
        raise ValueError(f"source_kbps must be positive, got {source_kbps}")
    ceiling = math.floor(source_kbps)
    for cbr in MP3_CBR_LADDER:
        if cbr <= ceiling:
            return cbr
    raise StemSizeError(
        f"no rung in MP3_CBR_LADDER={MP3_CBR_LADDER} fits under source_kbps="
        f"{source_kbps} (floor={ceiling}); the ladder floors at "
        f"{MP3_CBR_LADDER[-1]} kbps -- this is a policy gap, not a guess."
    )


def lossy_stem_size_ratio(*, source_bytes: int, part_bytes: int) -> float:
    """part_bytes / source_bytes -- 1.0 means an exact match, > 1.0 means
    over source. Recorded per stem (whether or not it violates) so
    meta.json/status reporting always has the number, not just a bool."""
    if source_bytes <= 0:
        raise StemSizeError(f"source_bytes must be positive, got {source_bytes}")
    return part_bytes / source_bytes


@dataclass(frozen=True)
class StemSizeViolation:
    """One lossy stem part that landed over the source file's bytes.

    A KPI/target miss to record, not a reason to discard the file -- see
    the module docstring's "Size-vs-source is a KPI/TARGET, not a hard
    gate" section.
    """

    name: str
    part_bytes: int
    source_bytes: int
    ratio: float  # lossy_stem_size_ratio(...) -- always > 1.0 here


def assert_lossy_stems_not_larger_than_source(
    *,
    source_bytes: int,
    part_sizes: Mapping[str, int] | Iterable[tuple[str, int]],
    codec: str,
) -> list[StemSizeViolation]:
    """Every lossy durable stem part that is larger than the source file,
    as a list of StemSizeViolation -- empty when every part fits.

    Size-vs-source is a KPI/target, not a hard gate (see the module
    docstring): this function used to raise StemSizeError on an offender;
    it now RETURNS the offenders instead, so the caller can keep the file
    and record the overage rather than discard a separation that took real
    GPU time to produce. The name kept its original "assert_" prefix
    (matching every existing call site) even though it no longer asserts --
    renaming would only churn imports for no behavioural gain.

    Control codecs (flac/wav) return an empty list unconditionally: they
    exist for scoring, where size was never the product constraint.
    A non-positive source_bytes is still a caller contract bug, not a size
    target miss, and still raises StemSizeError.
    """
    if codec in CONTROL_CODECS or codec not in LOSSY_SIZE_GATED:
        return []
    if source_bytes <= 0:
        raise StemSizeError(f"source_bytes must be positive, got {source_bytes}")
    items = part_sizes.items() if isinstance(part_sizes, Mapping) else part_sizes
    return [
        StemSizeViolation(
            name=name,
            part_bytes=size,
            source_bytes=source_bytes,
            ratio=lossy_stem_size_ratio(source_bytes=source_bytes, part_bytes=size),
        )
        for name, size in items
        if size > source_bytes
    ]
