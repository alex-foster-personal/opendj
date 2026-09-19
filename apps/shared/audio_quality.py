"""Audio quality as a venue you could get away with playing it at.

A bitrate means nothing to most people at a glance; "you could play this in a
lounge but not a stadium" means everything. Six rungs, named for the biggest
room the file survives.

WHY EFFECTIVE BITRATE, NOT THE HEADER. This is derived from file size over
duration, which needs no ffprobe and no per-file subprocess -- so it runs over
8000 tracks in milliseconds instead of minutes. It is also more honest than a
header for the case that actually matters here: a YouTube rip re-encoded to a
320 kbps container is a 320 kbps LIE, and its size-over-duration gives it away
where its header does not. The cost is that VBR reads as its average, which is
correct for this purpose.

Lossless is decided by CONTAINER, not by bitrate, because a quiet lossless
track can compress below a loud lossy one and rank absurdly otherwise.

MINI-PRD
--------
Status key: `→` out of scope | `?` todo | `✔︎` done | `✔︎ ✅` done + ran + works
as expected | `✔︎ ✅ 🎯` done + working + regression tests.

  ✔︎ ✅ 🎯 classify a file into exactly one of six venue rungs from size,
    duration and extension alone -- no subprocess, no decode.
    [if] a 3-minute file is 7.2 MB and .mp3 [then] ~320 kbps -> Warehouse
    [if] the same 3 minutes is 1.4 MB [then] ~64 kbps -> Naughty step
    [if] the extension is .flac [then] Stadium regardless of bitrate, because a
         quiet lossless master must not rank below a loud 320 kbps mp3
    [if] duration is 0 or missing [then ⛔️] return UNKNOWN, never a guess --
         dividing by a missing duration is how a plausible wrong number is born

  ✔︎ ✅ 🎯 never invent. An absent file, an absent duration and an unknown
    container are three different answers, all of them honest.
    [if] the file does not exist [then] UNKNOWN with reason "file missing"
    [if] the container is unrecognised [then] UNKNOWN with reason naming it

  → transcoding, repairing or replacing anything. This only reports.
  → true perceptual quality. A 320 kbps encode of a bad master is still a bad
    master, and no size-based measure can see that.

-Claude
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# ----- CFG -------------------------------------------------------------------
LOSSLESS_EXTS: frozenset[str] = frozenset(
    {".flac", ".wav", ".aiff", ".aif", ".alac", ".ape", ".wv"}
)
LOSSY_EXTS: frozenset[str] = frozenset(
    {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".mp4"}
)


@dataclass(frozen=True)
class Venue:
    """One rung. ``rank`` ascends with quality; 0 is the naughty step."""

    rank: int
    key: str
    label: str
    blurb: str  # goes in the hover title, per the numeric-readout house rule


# The ladder. Ascending, and the names are the point: a DJ knows instantly
# whether a file is a house-party file or a stadium file.
VENUES: tuple[Venue, ...] = (
    Venue(0, "naughty_step", "Naughty step",
          "under 128 kbps effective -- audibly rough, for emergencies only"),
    Venue(1, "lounge", "Lounge",
          "128-191 kbps -- fine at conversation volume, thin on a system"),
    Venue(2, "house_party", "House party",
          "192-255 kbps -- holds up on speakers, cymbals start to suffer"),
    Venue(3, "club", "Club",
          "256-319 kbps -- solid on a real rig"),
    Venue(4, "warehouse", "Warehouse",
          "320 kbps or better -- the practical ceiling for lossy"),
    Venue(5, "stadium", "Stadium",
          "lossless -- nothing thrown away, safe anywhere"),
)
BY_KEY: dict[str, Venue] = {v.key: v for v in VENUES}
# Upper bound (exclusive) of each lossy rung, in kbps. Lossless skips this.
LOSSY_CUTOFFS_KBPS: tuple[tuple[float, str], ...] = (
    (128, "naughty_step"),
    (192, "lounge"),
    (256, "house_party"),
    (320, "club"),
)
TOP_LOSSY: str = "warehouse"
LOSSLESS: str = "stadium"


@dataclass(frozen=True)
class Quality:
    """What we can honestly say about one file."""

    venue: Venue | None
    kbps: float | None
    container: str
    lossless: bool
    reason: str = ""  # only set when venue is None

    @property
    def known(self) -> bool:
        return self.venue is not None

    def as_dict(self) -> dict:
        return {
            "venue": self.venue.key if self.venue else None,
            "label": self.venue.label if self.venue else "Unknown",
            "rank": self.venue.rank if self.venue else None,
            "of": len(VENUES),
            "blurb": self.venue.blurb if self.venue else self.reason,
            "kbps": round(self.kbps) if self.kbps else None,
            "container": self.container,
            "lossless": self.lossless,
        }


# ----- classify --------------------------------------------------------------
def classify(
    file_path: str | None, duration_ms: int | None,
    size_bytes: int | None = None,
) -> Quality:
    """Venue rung for one file. Returns an UNKNOWN Quality rather than guessing.

    ``size_bytes`` may be passed to avoid a stat call when the caller already
    has it (bulk listings do).
    """
    if not file_path:
        return Quality(None, None, "", False, "no file path on this track")
    path = Path(file_path)
    ext = path.suffix.lower()
    if size_bytes is None:
        try:
            size_bytes = path.stat().st_size
        except OSError:
            return Quality(None, None, ext, ext in LOSSLESS_EXTS, "file missing")
    lossless = ext in LOSSLESS_EXTS
    if lossless:
        # Container decides. A quiet lossless master compresses smaller than a
        # loud 320 kbps mp3, so ranking it by bitrate would be nonsense.
        kbps = None
        if duration_ms:
            kbps = size_bytes * 8 / (duration_ms / 1000) / 1000
        return Quality(BY_KEY[LOSSLESS], kbps, ext, True)
    if ext not in LOSSY_EXTS:
        return Quality(None, None, ext, False, f"unrecognised container {ext!r}")
    if not duration_ms:
        return Quality(
            None, None, ext, False,
            "no duration, so effective bitrate cannot be computed",
        )
    kbps = size_bytes * 8 / (duration_ms / 1000) / 1000
    for cutoff, key in LOSSY_CUTOFFS_KBPS:
        if kbps < cutoff:
            return Quality(BY_KEY[key], kbps, ext, False)
    return Quality(BY_KEY[TOP_LOSSY], kbps, ext, False)


def ladder() -> list[dict]:
    """The whole ladder, for a UI legend."""
    return [
        {"rank": v.rank, "key": v.key, "label": v.label, "blurb": v.blurb}
        for v in VENUES
    ]
