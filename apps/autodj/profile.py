"""TrackProfile: one track as a planner needs it, built from what the app serves.

The inputs are the two documents the engine already answers for any track:
``GET /api/v1/tracks/{id}`` (title, artist, BPM, key, duration) and
``GET /api/v1/tracks/{id}/anlz`` (beatgrid, phrases, waveform bands, vocals,
key segments, cues). :func:`profile_from_payloads` is pure, so a profile is a
function of those two documents and nothing else.

Bars are 1-based and counted on the grid's DOWNBEATS (``n == 1``), which is
the same rule the page uses to resolve a ``bars`` Duration
(``agent-duration.ts`` ``_downbeats``), so a bar number here and a bar number
on the page name the same moment.

Per-bar energy and bass come from the waveform DETAIL bands, normalized to the
track's own loudest bar. They are relative (0..1 within the track), not LUFS:
the canon doc's "LUFS is not persisted" gap still stands, and nothing here
pretends otherwise. A mono waveform yields energy but no bass; an absent one
yields neither, and the fields say ``None`` rather than guessing.
"""

from __future__ import annotations

import bisect
import math
import statistics
from itertools import pairwise
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from apps.autodj.sections import UNKNOWN, phrase_label
from apps.shared.harmonic import key_to_camelot

#: Vocal coverage of a bar above which the bar counts as "has vocals".
VOCAL_BAR_THRESHOLD = 0.25


class ProfileError(ValueError):
    """The documents cannot describe a mixable track (no grid, no length)."""


@dataclass(frozen=True)
class Section:
    """One labelled phrase, on bars."""

    index: int
    label: str
    raw: str
    start_bar: int
    bars: int
    start_s: float
    end_s: float
    energy: float | None
    bass: float | None
    vocal_bars: int | None

    @property
    def end_bar(self) -> int:
        """The first bar AFTER this section."""
        return self.start_bar + self.bars

    def summary(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "raw": self.raw,
            "start_bar": self.start_bar,
            "bars": self.bars,
            "energy": _round(self.energy),
            "bass": _round(self.bass),
            "vocal_bars": self.vocal_bars,
        }


@dataclass(frozen=True)
class TrackProfile:
    """Compact musical facts about one track (``get_track_profile``)."""

    stable_id: str
    title: str | None
    artist: str | None
    bpm: float | None
    key: str | None
    duration_s: float
    downbeats_s: tuple[float, ...]
    grid_trusted: bool
    grid_note: str | None
    sections: tuple[Section, ...]
    sections_source: str
    bar_energy: tuple[float, ...] | None
    bar_bass: tuple[float, ...] | None
    bar_vocals: tuple[float, ...] | None
    vocals_status: str
    key_segments: tuple[dict[str, Any], ...] = ()
    hot_cues: tuple[dict[str, Any], ...] = ()
    stems_available: bool | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)
    #: AGENT-31: integrated loudness (``apps/loudness`` scan, the ``loudness_lufs`` lane), or None with ``lufs_reason``.
    lufs: float | None = None
    lufs_reason: str | None = None
    #: AGENT-51: the bar the user's transition-in cue sits on (Traktor Fade In,
    #: Mixxx intro start), or None when the track has none. ``mix_windows``
    #: makes it THE in window when present.
    transition_in_bar: int | None = None

    # ----- bar arithmetic -------------------------------------------------
    @property
    def bars(self) -> int:
        """How many whole bars the grid holds."""
        return len(self.downbeats_s)

    @property
    def bar_s(self) -> float:
        """Nominal seconds per bar (4 beats at the track's BPM)."""
        if self.bpm is not None and self.bpm > 0:
            return 240.0 / self.bpm
        if len(self.downbeats_s) >= 2:
            return statistics.median(b - a for a, b in zip(self.downbeats_s, self.downbeats_s[1:], strict=False))
        raise ProfileError(f"{self.stable_id}: no tempo to size a bar")

    def time_of_bar(self, bar: float) -> float:
        """Track seconds at a 1-based, possibly fractional, bar."""
        if not self.downbeats_s:
            raise ProfileError(f"{self.stable_id}: no downbeats")
        index = bar - 1.0
        whole = int(index // 1)
        frac = index - whole
        if whole < 0:
            return self.downbeats_s[0] + index * self.bar_s
        if whole >= len(self.downbeats_s) - 1:
            last = len(self.downbeats_s) - 1
            return self.downbeats_s[last] + (index - last) * self.bar_s
        start = self.downbeats_s[whole]
        return start + frac * (self.downbeats_s[whole + 1] - start)

    def bar_at(self, seconds: float) -> float:
        """1-based fractional bar at a track time (inverse of :meth:`time_of_bar`)."""
        if not self.downbeats_s:
            raise ProfileError(f"{self.stable_id}: no downbeats")
        first = self.downbeats_s[0]
        if seconds < first:
            return 1.0 + (seconds - first) / self.bar_s
        index = bisect.bisect_right(self.downbeats_s, seconds) - 1
        if index >= len(self.downbeats_s) - 1:
            last = len(self.downbeats_s) - 1
            return 1.0 + last + (seconds - self.downbeats_s[last]) / self.bar_s
        start, end = self.downbeats_s[index], self.downbeats_s[index + 1]
        return 1.0 + index + (seconds - start) / (end - start)

    def section_at(self, bar: float) -> Section | None:
        for section in self.sections:
            if section.start_bar <= bar < section.end_bar:
                return section
        return None

    def vocal_bars_between(self, start_bar: int, bars: int) -> int | None:
        """Bars in ``[start_bar, start_bar + bars)`` that carry vocals."""
        if self.bar_vocals is None:
            return None
        lo = max(0, start_bar - 1)
        hi = min(len(self.bar_vocals), start_bar - 1 + bars)
        return sum(1 for value in self.bar_vocals[lo:hi] if value >= VOCAL_BAR_THRESHOLD)

    def mean_between(self, series: tuple[float, ...] | None, start_bar: int, bars: int) -> float | None:
        if series is None:
            return None
        lo = max(0, start_bar - 1)
        hi = min(len(series), start_bar - 1 + bars)
        if hi <= lo:
            return None
        return sum(series[lo:hi]) / (hi - lo)

    def summary(self) -> dict[str, Any]:
        """The compact JSON an agent reads; never a full beatgrid or waveform."""
        return {
            "stable_id": self.stable_id,
            "title": self.title,
            "artist": self.artist,
            "bpm": self.bpm,
            "key": self.key,
            "duration_s": round(self.duration_s, 2),
            "bars": self.bars,
            "grid_trusted": self.grid_trusted,
            "grid_note": self.grid_note,
            "sections_source": self.sections_source,
            "sections": [section.summary() for section in self.sections],
            "vocals_status": self.vocals_status,
            "key_segments": list(self.key_segments),
            "hot_cues": list(self.hot_cues),
            "stems_available": self.stems_available,
            "loudness": {"lufs": self.lufs, "reason": self.lufs_reason},
            "transition_in_bar": self.transition_in_bar,
            "notes": list(self.notes),
        }


def _round(value: float | None, places: int = 2) -> float | None:
    return None if value is None else round(value, places)


def _camelot(raw: object) -> str | None:
    """The Camelot key, None when the track has no key; a key that does not parse refuses the profile, never reads as no key."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return str(key_to_camelot(raw))
    except ValueError as error:
        raise ProfileError(f"unrecognized key {raw!r}") from error


def _downbeats(beats: Sequence[Mapping[str, Any]]) -> tuple[float, ...]:
    return tuple(float(beat["t"]) for beat in beats if beat.get("n") == 1)


def _grid_bpm(beats: Sequence[Mapping[str, Any]]) -> float | None:
    bpms = [float(beat["bpm"]) for beat in beats if isinstance(beat.get("bpm"), (int, float))]
    bpms = [bpm for bpm in bpms if bpm > 0]
    return round(statistics.median(bpms), 2) if bpms else None


def _at(series: Sequence[float], index: int) -> float:
    return float(series[index]) if index < len(series) else 0.0


def _bar_spans(downbeats: tuple[float, ...], bar_s: float, duration_s: float, length: int) -> list[range]:
    """The waveform sample indices each bar covers (at least one sample per bar)."""
    spans: list[range] = []
    for bar_index, start in enumerate(downbeats):
        end = downbeats[bar_index + 1] if bar_index + 1 < len(downbeats) else start + bar_s
        lo = min(length - 1, max(0, int(start / duration_s * length)))
        hi = min(length, max(lo + 1, int(end / duration_s * length)))
        spans.append(range(lo, hi))
    return spans


def _has_bands(low: Sequence[float], mid: Sequence[float], high: Sequence[float], length: int) -> bool:
    """A real three-band waveform: low differs from mid somewhere (a mono one repeats it)."""
    if not low or not (mid or high):
        return False
    return any(_at(low, i) != _at(mid, i) for i in range(0, length, max(1, length // 64)))


def _normalized(values: list[float]) -> tuple[float, ...]:
    """``values`` over their peak (a silent series stays at zero)."""
    peak = max(values) or 1.0
    return tuple(value / peak for value in values)


def _band_series(
    detail: Mapping[str, Any], duration_s: float, downbeats: tuple[float, ...], bar_s: float
) -> tuple[tuple[float, ...] | None, tuple[float, ...] | None]:
    """Per-bar (energy, bass), each normalized to the track's loudest bar."""
    low, mid, high = (list(detail.get(band) or []) for band in ("low", "mid", "high"))
    length = max(len(low), len(mid), len(high))
    if length == 0 or duration_s <= 0 or not downbeats:
        return None, None
    energy: list[float] = []
    bass: list[float] = []
    for span in _bar_spans(downbeats, bar_s, duration_s, length):
        energy.append(sum(_at(low, i) + _at(mid, i) + _at(high, i) for i in span) / len(span))
        bass.append(sum(_at(low, i) for i in span) / len(span))
    return _normalized(energy), _normalized(bass) if _has_bands(low, mid, high, length) else None


def _vocal_series(
    vocals: Mapping[str, Any], downbeats: tuple[float, ...], bar_s: float
) -> tuple[str, tuple[float, ...] | None]:
    status = str(vocals.get("status") or "not_analyzed")
    if status == "not_analyzed" or not downbeats:
        return status, None
    regions = [
        (float(region["start_s"]), float(region["end_s"]))
        for region in vocals.get("regions") or []
        if isinstance(region, Mapping)
    ]
    coverage: list[float] = []
    for bar_index, start in enumerate(downbeats):
        end = downbeats[bar_index + 1] if bar_index + 1 < len(downbeats) else start + bar_s
        covered = sum(max(0.0, min(end, r_end) - max(start, r_start)) for r_start, r_end in regions)
        coverage.append(min(1.0, covered / max(end - start, 1e-6)))
    return status, tuple(coverage)


def _nearest_bar(downbeats: tuple[float, ...], seconds: float) -> int:
    index = bisect.bisect_left(downbeats, seconds)
    if index >= len(downbeats):
        return len(downbeats)
    if index > 0 and seconds - downbeats[index - 1] < downbeats[index] - seconds:
        return index
    return index + 1


def _end_bar(downbeats: tuple[float, ...], seconds: float) -> int:
    """The exclusive bar a phrase ends on: past the last downbeat it keeps the final bar."""
    if downbeats and seconds > downbeats[-1]:
        return len(downbeats) + 1
    return _nearest_bar(downbeats, seconds)


def _sections(
    phrases: Sequence[Mapping[str, Any]],
    downbeats: tuple[float, ...],
    energy: tuple[float, ...] | None,
    bass: tuple[float, ...] | None,
    vocals: tuple[float, ...] | None,
) -> tuple[Section, ...]:
    rows: list[Section] = []
    starts = [_nearest_bar(downbeats, float(phrase["start_s"])) for phrase in phrases]
    total_bars = len(downbeats)
    for index, phrase in enumerate(phrases):
        start_bar = starts[index]
        end_bar = (
            starts[index + 1]
            if index + 1 < len(starts)
            else _end_bar(downbeats, float(phrase.get("end_s", phrase["start_s"])))
        )
        end_bar = min(max(end_bar, start_bar), total_bars + 1)
        bars = end_bar - start_bar
        if bars <= 0:
            continue
        raw, label = phrase_label(dict(phrase))
        lo, hi = start_bar - 1, start_bar - 1 + bars

        def mean(series: tuple[float, ...] | None, lo: int = lo, hi: int = hi) -> float | None:
            if series is None or hi <= lo:
                return None
            window = series[lo:hi]
            return sum(window) / len(window) if window else None

        rows.append(
            Section(
                index=len(rows),
                label=label,
                raw=raw,
                start_bar=start_bar,
                bars=bars,
                start_s=float(phrase["start_s"]),
                end_s=float(phrase.get("end_s", phrase["start_s"])),
                energy=mean(energy),
                bass=mean(bass),
                vocal_bars=(
                    None if vocals is None else sum(1 for value in vocals[lo:hi] if value >= VOCAL_BAR_THRESHOLD)
                ),
            )
        )
    return tuple(rows)


def _hot_cues(cues: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    return tuple(
        {"slot": cue.get("slot"), "in_ms": cue.get("in_ms"), "comment": cue.get("comment")}
        for cue in cues
        if cue.get("kind") == "hot_cue"
    )


def _transition_in_bar(
    stable_id: str, cues: Sequence[Mapping[str, Any]], downbeats: tuple[float, ...]
) -> int | None:
    """The bar of the track's ``transition_in`` cue (AGENT-51), or None.

    The store saves the cue ON a downbeat, but the grid can move after that
    (a re-analysis, a grid edit), so the stored time is snapped again here to
    the nearest downbeat of the grid this profile reads, never trusted as a bar.
    """
    found = [cue for cue in cues if cue.get("kind") == "transition_in"]
    if not found:
        return None
    if len(found) > 1:
        raise ProfileError(f"{stable_id}: {len(found)} transition-in cues; the store keeps one")
    in_ms = found[0].get("in_ms")
    if not isinstance(in_ms, (int, float)) or isinstance(in_ms, bool) or in_ms < 0:
        raise ProfileError(f"{stable_id}: transition-in cue has no position ({in_ms!r})")
    return _nearest_bar(downbeats, float(in_ms) / 1000.0)


def downbeats_of(stable_id: str, anlz: Mapping[str, Any]) -> tuple[float, ...]:
    """The downbeats (seconds) a profile of ``anlz`` would count bars on.

    Raises :class:`ProfileError` when the grid has fewer than two downbeats.
    """
    return _grid(stable_id, anlz.get("beatgrid") or {})[1]


def snap_to_downbeat(downbeats: tuple[float, ...], seconds: float) -> tuple[int, float]:
    """``(bar, downbeat_seconds)`` of the downbeat nearest ``seconds`` (1-based bar)."""
    bar = _nearest_bar(downbeats, seconds)
    return bar, downbeats[bar - 1]


def _key_segments(anlz: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    block = anlz.get("key_segments")
    if not isinstance(block, Mapping) or block.get("status") != "ok":
        return ()
    # The analysis lane counts bars from zero and its segments are half-open; the profile's bars are 1-based and
    # inclusive, so a segment [s, e) covers profile bars s+1 through e.
    return tuple(
        {
            "start_bar": segment["start_bar"] + 1 if _is_bar(segment.get("start_bar")) else segment.get("start_bar"),
            "end_bar": segment.get("end_bar"),
            "key": segment.get("key_camelot"),
            "confidence": segment.get("confidence"),
        }
        for segment in block.get("segments") or []
        if isinstance(segment, Mapping)
    )


def _is_bar(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _records(value: Any) -> list[Mapping[str, Any]]:
    """The mapping rows of a payload list (absent or malformed rows dropped)."""
    return [row for row in value or [] if isinstance(row, Mapping)]


def _grid(stable_id: str, beatgrid: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], tuple[float, ...]]:
    """The beats and downbeats; fewer than two downbeats cannot be mixed on bars."""
    beats = _records(beatgrid.get("beats"))
    downbeats = _downbeats(beats)
    if len(downbeats) < 2:
        raise ProfileError(
            f"{stable_id}: the beatgrid has {len(downbeats)} downbeats "
            f"(status {beatgrid.get('status', 'ok')!r}); a track with no grid cannot be "
            "mixed on bars"
        )
    return beats, downbeats


def _grid_trust(beatgrid: Mapping[str, Any]) -> tuple[bool, str | None]:
    """Whether bar positions can be trusted, and why not when they cannot."""
    grid_status = beatgrid.get("status", "ok")
    if grid_status == "ok" and not beatgrid.get("static_grid_untrusted", False):
        return True, None
    return False, str(beatgrid.get("reason") or f"grid status {grid_status!r}")


def _timing(
    track: Mapping[str, Any], beats: list[Mapping[str, Any]], downbeats: tuple[float, ...], notes: list[str]
) -> tuple[float | None, float, float]:
    """(bpm, duration_s, bar_s): the library row first, the grid when the row has none."""
    bpm = track.get("bpm")
    bpm = float(bpm) if isinstance(bpm, (int, float)) and bpm > 0 else _grid_bpm(beats)
    duration_ms = track.get("duration_ms")
    if isinstance(duration_ms, (int, float)) and duration_ms > 0:
        duration_s = float(duration_ms) / 1000.0
    else:
        duration_s = float(beats[-1]["t"]) + (60.0 / bpm if bpm else 0.5)
        notes.append("duration taken from the last beat: the library row has none")
    bar_s = 240.0 / bpm if bpm else statistics.median(b - a for a, b in pairwise(downbeats))
    return bpm, duration_s, bar_s


def _analysis_notes(
    energy: tuple[float, ...] | None,
    bass: tuple[float, ...] | None,
    vocal_series: tuple[float, ...] | None,
    sections: tuple[Section, ...],
) -> list[str]:
    """What the analysis could not say, in the words a planner reads."""
    notes: list[str] = []
    if energy is None:
        notes.append("no waveform detail: energy and bass are unknown")
    elif bass is None:
        notes.append("mono waveform: bass per bar is unknown")
    if vocal_series is None:
        notes.append("vocals not analyzed: vocal clashes cannot be checked")
    if not sections:
        notes.append("no phrase analysis: windows fall back to the first and last bars")
    elif all(section.label == UNKNOWN for section in sections):
        notes.append("phrases present but none could be labelled")
    return notes


def _text(track: Mapping[str, Any], key: str) -> str | None:
    value = track.get(key)
    return value if isinstance(value, str) else None


def profile_from_payloads(anlz: Mapping[str, Any], track: Mapping[str, Any] | None = None) -> TrackProfile:
    """Build a profile from ``/anlz`` and (optionally) the library row."""
    track = track or {}
    stable_id = str(anlz.get("stable_id") or track.get("stable_id") or "")
    if not stable_id:
        raise ProfileError("anlz payload names no stable_id")
    beatgrid = anlz.get("beatgrid") or {}
    beats, downbeats = _grid(stable_id, beatgrid)
    grid_trusted, grid_note = _grid_trust(beatgrid)
    notes: list[str] = []
    bpm, duration_s, bar_s = _timing(track, beats, downbeats, notes)
    waveform = anlz.get("waveform") or {}
    energy, bass = _band_series(waveform.get("detail") or {}, duration_s, downbeats, bar_s)
    vocals_status, vocal_series = _vocal_series(anlz.get("vocals") or {}, downbeats, bar_s)
    phrases = _records(anlz.get("phrases"))
    sections = _sections(phrases, downbeats, energy, bass, vocal_series)
    sections_source = str(anlz.get("phrases_source") or ("rekordbox" if phrases else "none"))
    notes += _analysis_notes(energy, bass, vocal_series, sections)
    stems = track.get("stems_available")
    return TrackProfile(
        stable_id=stable_id,
        title=_text(track, "title"),
        artist=_text(track, "artist"),
        bpm=bpm,
        key=_camelot(track.get("key")),
        duration_s=duration_s,
        downbeats_s=downbeats,
        grid_trusted=grid_trusted,
        grid_note=grid_note,
        sections=sections,
        sections_source=sections_source,
        bar_energy=energy,
        bar_bass=bass,
        bar_vocals=vocal_series,
        vocals_status=vocals_status,
        key_segments=_key_segments(anlz),
        hot_cues=_hot_cues(_records(anlz.get("cues"))),
        transition_in_bar=_transition_in_bar(stable_id, _records(anlz.get("cues")), downbeats),
        stems_available=stems if isinstance(stems, bool) else None,
        notes=tuple(notes),
        **_loudness(track),
    )


def _finite_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _loudness(track: Mapping[str, Any]) -> dict[str, Any]:
    """The single-track read's ``loudness`` block: LUFS, or None with its reason (never a default loudness)."""
    if "loudness" not in track:
        # A row with no block at all (an offline profile, or an older engine) says so; it is not a measurement.
        return {"lufs": None, "lufs_reason": "the track read carries no loudness block"}
    block = track["loudness"]
    if not isinstance(block, Mapping) or set(block) != {"lufs", "status", "reason"}:
        raise ProfileError(f"malformed loudness block {block!r}: want lufs, status and reason")
    value, status, reason = block["lufs"], block["status"], block["reason"]
    if status == "ok":
        if not _finite_number(value) or reason is not None:
            raise ProfileError(f"malformed loudness block {dict(block)!r}: ok needs a finite lufs and no reason")
        return {"lufs": float(value), "lufs_reason": None}
    if status in ("failed", "missing"):
        if value is not None or not isinstance(reason, str) or not reason:
            raise ProfileError(f"malformed loudness block {dict(block)!r}: {status} needs a null lufs and a reason")
        return {"lufs": None, "lufs_reason": reason}
    if status == "error":
        raise ProfileError(f"the engine's loudness store is broken: {reason}")
    raise ProfileError(f"malformed loudness block {dict(block)!r}: unknown status {status!r}")


#: The TRIM knob's top gain: the engine scales linearly by ``2 * knob`` (``TRIM_MAX_GAIN``, player/constants.ts), so
#: unity sits at 0.5 and the most a trim can add is 20*log10(2) dB.
TRIM_MAX_GAIN = 2.0
TRIM_MAX_DB = 20.0 * math.log10(TRIM_MAX_GAIN)


def match_trim_db(
    out_profile: TrackProfile, in_profile: TrackProfile, out_trim_db: float = 0.0
) -> tuple[float | None, str]:
    """AGENT-31: the canon's match-the-outgoing trim (canon AutoDJ doc step 7): incoming gain that puts it at the
    outgoing deck's PLAYING level, its integrated LUFS plus the outgoing trim ``out_trim_db`` (Codex #7594: else
    consecutive transitions drift by every earlier trim). None, with the reason, when either LUFS is unknown;
    capped at the knob's +6.02 dB, saying so."""
    if out_profile.lufs is None or in_profile.lufs is None:
        missing = out_profile if out_profile.lufs is None else in_profile
        return None, f"no trim: {missing.stable_id} loudness unknown ({missing.lufs_reason})"
    if not math.isfinite(out_trim_db):
        raise ValueError(f"outgoing trim {out_trim_db} dB is not a number")
    reference = out_profile.lufs + out_trim_db
    wanted = reference - in_profile.lufs
    if out_trim_db != 0.0:
        return _capped(
            wanted,
            f"match {in_profile.lufs:.1f} to {out_profile.lufs:.1f} LUFS with the outgoing trim "
            f"{out_trim_db:+.1f} dB ({reference:.1f} LUFS)",
        )
    return _capped(wanted, f"match {in_profile.lufs:.1f} to {out_profile.lufs:.1f} LUFS")


def _capped(wanted: float, what: str) -> tuple[float, str]:
    if wanted > TRIM_MAX_DB:
        return round(TRIM_MAX_DB, 2), f"{what} wants {wanted:+.1f} dB; capped at the trim's {TRIM_MAX_DB:+.2f} dB"
    return round(wanted, 2), f"{what}: {wanted:+.1f} dB"


def trim_db_of_knob(knob: float) -> float:
    """The gain in dB of a TRIM knob value (0.5 unity); a knob at 0 is silence, which no loudness match can use."""
    if not 0.0 < knob <= 1.0:
        raise ValueError(f"trim knob {knob} gives no finite gain")
    return 20.0 * math.log10(knob * TRIM_MAX_GAIN)


#: Round 21 (ADR-0135): every deck loads at this headroom pre-gain, so two decks of masters near 0 dBFS can overlap
#: under the -1 dBTP ceiling. Mimicked, not chosen: Mixxx's ReplayGain "initial boost without ReplayGain data"
#: (`[ReplayGain] DefaultBoost`) resets to -6 dB (mixxx 2.4 src/preferences/dialog/dlgprefreplaygain.cpp,
#: slotResetToDefaults: `SliderDefaultBoost->setValue(-6)`).
HEADROOM_DB = -6.0
#: Every deck loads at the same headroom default as the live mixer.
DECK_LOAD_TRIM_DB = HEADROOM_DB


def trim_knob(trim_db: float) -> float:
    """The TRIM knob value (0..1, 0.5 unity) for a gain in dB; raises past the knob's top."""
    if trim_db > TRIM_MAX_DB + 1e-9:
        raise ValueError(f"trim {trim_db:+.2f} dB is past the knob's {TRIM_MAX_DB:+.2f} dB")
    return round(10.0 ** (trim_db / 20.0) / TRIM_MAX_GAIN, 6)
