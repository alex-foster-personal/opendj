"""Read a rekordbox XML library (``DJ_PLAYLISTS``) into an ``OpenDjLibrary``.

The layout, as rekordbox documents it and as Lexicon, Engine DJ and VirtualDJ
write it::

    <DJ_PLAYLISTS Version="1.0.0">
      <PRODUCT Name="rekordbox" Version="6.8.5" Company="AlphaTheta"/>
      <COLLECTION Entries="N">
        <TRACK TrackID="1" Name=".." Artist=".." Album=".." TotalTime="242"
               AverageBpm="128.00" Tonality="Am" Rating="204" PlayCount="7"
               Comments=".." Colour="0xFF007F"
               Genre="House" Label=".." Year="2019" DateAdded="2026-11-08"
               BitRate="320" SampleRate="44100" Size="9812345"
               Location="file://localhost/Volumes/Invented%20DJ/a.mp3">
          <TEMPO Inizio="0.025" Bpm="128.00" Metro="4/4" Battito="1"/>
          <POSITION_MARK Name="Drop" Type="0" Start="32.0" Num="2"/>
        </TRACK>
      </COLLECTION>
      <PLAYLISTS>
        <NODE Type="0" Name="ROOT" Count="1">
          <NODE Name="Warmup" Type="1" KeyType="0" Entries="1">
            <TRACK Key="1"/>
          </NODE>
        </NODE>
      </PLAYLISTS>
    </DJ_PLAYLISTS>

``POSITION_MARK`` ``Type`` is 0 cue, 1 fade-in, 2 fade-out, 3 load, 4 loop;
``Num`` is the hot-cue pad (0-7) or -1 for a memory cue. ``Start`` and ``End``
are seconds. ``Rating`` is 0, 51, 102, 153, 204 or 255 for 0-5 stars. A
playlist's ``KeyType`` is 0 when ``Key`` is a ``TrackID`` and 1 when it is a
``Location``; a NODE's ``Type`` is 0 for a folder and 1 for a playlist.
Anything else in either attribute stops the read. Descriptive TRACK attributes
become ``x_*`` extensions: stripped strings; Year, BitRate, SampleRate and Size
as ints; DateAdded as an ISO ``YYYY-MM-DD`` string.

Parsing uses the standard library's expat, whose entity-expansion limits
(expat 2.4 and later) refuse a "billion laughs" document.
"""

from __future__ import annotations

import datetime as _dt
import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import unquote, urlsplit

from apps.open_dj import BeatGridPoint, CuePoint, OpenDjLibrary, Playlist, Track
from apps.open_dj.schema import CueType

_MARK_TYPES: dict[str, CueType] = {
    "0": "hot",
    "1": "fade_in",
    "2": "fade_out",
    "3": "load",
    "4": "loop",
}

#: Hot-cue pads: ``Num`` 0-7 is a pad, -1 a memory cue.
_PADS = 8
#: ``Rating`` encodes 0-5 stars in steps of 51.
_RATINGS = {stars * 51: stars for stars in range(6)}
#: TRACK attributes kept verbatim (stripped) as extensions.
_TEXT_EXTENSIONS = (
    ("Genre", "x_genre"),
    ("Label", "x_label"),
    ("Remixer", "x_remixer"),
    ("Composer", "x_composer"),
    ("Grouping", "x_grouping"),
    ("Mix", "x_mix"),
)
#: Whole-number TRACK attributes kept as int extensions. rekordbox writes 0 for unknown.
_INT_EXTENSIONS = (
    ("Year", "x_year"),
    ("BitRate", "x_bitrate"),
    ("SampleRate", "x_sample_rate"),
    ("Size", "x_size"),
)
_DATEADDED_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class RekordboxXml:
    """A parsed export: the library, and which app wrote it."""

    library: OpenDjLibrary
    #: ``PRODUCT/@Name`` ("rekordbox", "Lexicon", ...), or "" when absent.
    product: str
    #: TRACK elements with no usable ``Location`` (streaming, or blank).
    tracks_without_location: int


def read_rekordbox_xml(source: Path) -> RekordboxXml:
    """Parse ``source``. Raises ``ValueError`` when it is not a DJ_PLAYLISTS file."""
    root = ET.parse(Path(source)).getroot()
    if root.tag != "DJ_PLAYLISTS":
        raise ValueError(f"{source} is not a rekordbox XML library (root <{root.tag}>)")
    product_el = root.find("PRODUCT")
    product = (product_el.get("Name") or "") if product_el is not None else ""
    collections = root.findall("COLLECTION")
    if len(collections) > 1:
        # Only the first would be read; the rest would drop their tracks without a word.
        raise ValueError(f"{source} has {len(collections)} <COLLECTION> sections, expected one")
    collection = collections[0] if collections else None
    if collection is None:
        # Every writer emits COLLECTION, even for an empty library; without it
        # the file is truncated or not an export, never "no tracks".
        raise ValueError(f"{source} has no <COLLECTION>: truncated or not a rekordbox XML export")
    tracks, by_location, track_ids, without_location = _read_collection(source, collection)
    playlist_sections = root.findall("PLAYLISTS")
    if len(playlist_sections) > 1:
        # Only the first would be read; the rest would drop their playlists without a word.
        raise ValueError(f"{source} has {len(playlist_sections)} <PLAYLISTS> sections, expected at most one")
    playlists_el = playlist_sections[0] if playlist_sections else None
    playlists = _read_playlists(source, playlists_el, by_location, track_ids)
    return RekordboxXml(
        library=OpenDjLibrary(version="0.1", tracks=tuple(tracks), playlists=playlists),
        product=product,
        tracks_without_location=without_location,
    )


def _read_collection(
    source: Path, collection: ET.Element
) -> tuple[list[Track], dict[str, str], set[str], int]:
    """The collection's tracks, and what a playlist entry can name them by.

    ``by_location`` maps each ``Location`` to its ``rbxml_`` id and ``track_ids``
    holds every ``rbxml_`` id, including skipped tracks. A skipped track has no
    file to import, so a playlist entry that names one keeps its id, which the
    ingest counts as unresolved rather than dropping it silently.
    """
    tracks: list[Track] = []
    by_location: dict[str, str] = {}
    track_ids: set[str] = set()
    seen_ids: dict[str, str] = {}
    seen_locations: dict[str, str] = {}
    without_location = 0
    entries = collection.findall("TRACK")
    _refuse_count_mismatch(source, "COLLECTION", collection, len(entries))
    for el in entries:
        try:
            track = _read_track(el)
        except ValueError as exc:
            # Fail fast: a half-read library reported as a success hides loss.
            raise ValueError(f"{source}: TRACK TrackID={el.get('TrackID')!r}: {exc}") from exc
        raw_id = (el.get("TrackID") or "").strip()
        track_id = "rbxml_" + raw_id
        location = el.get("Location") or ""
        track_ids.add(track_id)
        if track_id in seen_ids:
            # Checked for every TRACK, skipped ones too: a TrackID-keyed playlist entry
            # would otherwise resolve the shared id to whichever file was read last.
            raise ValueError(
                f"{source}: TrackID={raw_id!r} is on two tracks, "
                f"{seen_ids[track_id]!r} and {location!r}"
            )
        seen_ids[track_id] = location
        if location:
            # Checked for every TRACK, skipped ones too: a Location-keyed playlist
            # entry would otherwise resolve to whichever id came first.
            if location in seen_locations:
                raise ValueError(
                    f"{source}: Location={location!r} is on two tracks, "
                    f"TrackID={seen_locations[location]!r} and TrackID={raw_id!r}"
                )
            seen_locations[location] = raw_id
        if track is None:
            without_location += 1
            by_location.setdefault(location, track_id)
            continue
        tracks.append(track)
        by_location[location] = track.track_id
    return tracks, by_location, track_ids, without_location


def _read_playlists(
    source: Path,
    playlists_el: ET.Element | None,
    by_location: dict[str, str],
    track_ids: set[str],
) -> tuple[Playlist, ...]:
    if playlists_el is None:
        return ()
    roots = playlists_el.findall("NODE")
    if len(roots) != 1:
        # Zero roots would read as an empty playlist set and more than one would
        # drop every root after the first; either is a partial import reported as whole.
        raise ValueError(f"{source}: PLAYLISTS holds {len(roots)} root NODEs, expected exactly one")
    top = roots[0]
    # The top node is the unnamed ROOT folder; its children are the
    # user's top-level folders and playlists. A root that is not a
    # folder is checked like any other node rather than skipped over.
    top_type = top.get("Type")
    if (top_type or "").strip() != "0":
        raise ValueError(
            f"{source}: PLAYLISTS: root NODE Name={top.get('Name')!r}: "
            f"Type={top_type!r} is not 0 (folder)"
        )
    _refuse_count_mismatch(
        source, f"PLAYLISTS: root NODE Name={top.get('Name')!r}", top, len(top.findall("NODE")), attr="Count"
    )
    return tuple(_read_nodes(top, by_location, track_ids))


def location_to_path(location: str) -> str | None:
    """``file://localhost/Volumes/Invented%20DJ/a.mp3`` -> ``/Volumes/Invented DJ/a.mp3``.

    Windows locations (``file://localhost/C:/Music/a.mp3``) come back as
    ``C:/Music/a.mp3``, and a network share (``file://nas/share/a.mp3``) as the
    UNC path ``//nas/share/a.mp3``, never as a local ``/share/a.mp3``.
    Anything that is not a ``file:`` URL is ``None``.
    """
    if not location:
        return None
    parts = urlsplit(location)
    if parts.scheme != "file":
        return None
    path = unquote(parts.path)
    host = unquote(parts.netloc)
    if host and host.lower() != "localhost":
        return f"//{host}{path}"
    if len(path) >= 3 and path[0] == "/" and path[2] == ":" and path[1].isalpha():
        return path[1:]
    return path or None


def _track_id_of(el: ET.Element) -> str:
    raw_track_id = (el.get("TrackID") or "").strip()
    if not raw_track_id:
        # Checked before the location skip, so a streaming TRACK with no id is
        # refused too. A playlist keyed by TrackID cannot resolve a substituted
        # identifier, so a malformed export must fail here rather than drop
        # membership later.
        raise ValueError(f"TRACK Location={el.get('Location')!r} has no TrackID")
    return "rbxml_" + raw_track_id


def _read_track(el: ET.Element) -> Track | None:
    track_id = _track_id_of(el)
    path = location_to_path(el.get("Location") or "")
    if path is None:
        return None
    artist = (el.get("Artist") or "").strip()
    extensions = _track_extensions(el)
    return Track(
        track_id=track_id,
        file_path=path,
        title=el.get("Name") or "",
        artists=(artist,) if artist else (),
        album=el.get("Album") or "",
        bpm=_track_bpm(el.get("AverageBpm")),
        key_camelot=(el.get("Tonality") or "").strip() or None,
        rating=_stars(el.get("Rating"), "Rating"),
        duration_ms=_ms(el.get("TotalTime"), "TotalTime"),
        # Track.play_count is a plain int defaulting to 0, and the import
        # writes no play counts; a count below zero is malformed, not zero.
        play_count=_count(el.get("PlayCount"), "PlayCount"),
        color_rgb=_colour(el.get("Colour"), "Colour"),
        cues=tuple(_read_marks(el)),
        beats=tuple(_read_tempo(el)),
        extensions=extensions,
    )


def _track_extensions(el: ET.Element) -> dict[str, object]:
    extensions: dict[str, object] = {}
    comment = (el.get("Comments") or "").strip()
    if comment:
        extensions["x_comment"] = comment
    for attr, key in _TEXT_EXTENSIONS:
        text = (el.get(attr) or "").strip()
        if text:
            extensions[key] = text
    for attr, key in _INT_EXTENSIONS:
        raw = el.get(attr)
        if raw is None or not raw.strip():
            continue
        value = _count(raw, attr)
        if value > 0:
            extensions[key] = value
    date_added = _date(el.get("DateAdded"), "DateAdded")
    if date_added is not None:
        extensions["x_date_added"] = date_added
    return extensions


def _date(raw: str | None, name: str) -> str | None:
    if raw is None or not raw.strip():
        return None
    text = raw.strip()
    if not _DATEADDED_RE.fullmatch(text):
        raise ValueError(f"{name}={raw!r} is not a YYYY-MM-DD date")
    try:
        parsed = _dt.date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not a YYYY-MM-DD date") from None
    return parsed.isoformat()


def _track_bpm(raw: str | None) -> float | None:
    """rekordbox writes ``AverageBpm="0.00"`` for a track it has not analyzed,
    so zero reads as missing; a negative BPM is malformed."""
    bpm = _float(raw, "AverageBpm")
    if bpm is not None and bpm < 0:
        raise ValueError(f"AverageBpm={raw!r} is negative")
    return bpm or None


def _read_marks(el: ET.Element) -> Iterator[CuePoint]:
    pads: set[int] = set()
    for index, mark in enumerate(el.findall("POSITION_MARK")):
        cue = _read_mark(mark, index)
        # Only a pad cue is type "hot"; two marks on one pad would load as
        # two cues in one slot, which the cue store refuses on every read.
        if cue.type == "hot":
            if cue.index in pads:
                raise ValueError(f"two POSITION_MARKs take hot-cue pad {cue.index}")
            pads.add(cue.index)
        yield cue


def _read_mark(mark: ET.Element, index: int) -> CuePoint:
    raw_type = mark.get("Type")
    cue_type = _MARK_TYPES.get((raw_type or "").strip())
    if cue_type is None:
        raise ValueError(f"POSITION_MARK Type={raw_type!r} is not a rekordbox mark type (0-4)")
    start, end = _mark_span(mark, cue_type)
    num = _int(mark.get("Num"), "Num")
    if num is not None and not -1 <= num < _PADS:
        raise ValueError(f"Num={mark.get('Num')!r} is neither -1 (memory) nor a pad 0-{_PADS - 1}")
    on_pad = num is not None and num >= 0
    length_ms = (end - start) if cue_type == "loop" and end is not None else None
    return CuePoint(
        index=num if num is not None and on_pad else index,
        position_ms=start,
        type=_kind_on_pad(cue_type, on_pad=on_pad),
        name=mark.get("Name") or "",
        color_rgb=_mark_rgb(mark),
        length_ms=length_ms,
    )


def _kind_on_pad(cue_type: CueType, *, on_pad: bool) -> CueType:
    if cue_type == "hot" and not on_pad:
        # Num -1 is a memory cue. A cue with no Num names no pad, so it
        # stays a memory cue rather than taking a pad it never had.
        return "memory"
    if cue_type == "loop" and on_pad:
        # A loop saved on a pad is a hot loop: it keeps its pad as a hot
        # cue that carries the loop's length, instead of losing the pad.
        return "hot"
    return cue_type


def _mark_span(mark: ET.Element, cue_type: CueType) -> tuple[int, int | None]:
    """A mark's Start and End in ms; a loop's End must come after its Start."""
    start = _ms(mark.get("Start"), "Start")
    if start is None:
        raise ValueError("POSITION_MARK has no Start")
    end = _ms(mark.get("End"), "End")
    if cue_type == "loop" and (end is None or end <= start):
        raise ValueError(f"loop End={mark.get('End')!r} is not after Start={mark.get('Start')!r}")
    return start, end


def _mark_rgb(mark: ET.Element) -> int | None:
    """A cue's color: all of Red, Green and Blue, each 0-255, or none of them."""
    names = ("Red", "Green", "Blue")
    given = [(name, _int(mark.get(name), name)) for name in names]
    channels = [(name, value) for name, value in given if value is not None]
    if not channels:
        return None
    if len(channels) != len(names):
        raise ValueError("Red, Green and Blue must be given together")
    rgb = 0
    for name, value in channels:
        if not 0 <= value <= 255:
            raise ValueError(f"{name}={mark.get(name)!r} is outside 0-255")
        rgb = rgb << 8 | value
    return rgb


def _read_tempo(el: ET.Element) -> Iterator[BeatGridPoint]:
    for tempo in el.findall("TEMPO"):
        start = _ms(tempo.get("Inizio"), "Inizio")
        bpm = _float(tempo.get("Bpm"), "Bpm")
        if start is None or bpm is None:
            raise ValueError("TEMPO needs both Inizio and Bpm")
        if bpm <= 0:
            raise ValueError(f"TEMPO Bpm={tempo.get('Bpm')!r} is not positive")
        yield BeatGridPoint(position_ms=start, bpm=bpm)


def _resolve_entries(
    name: str, node: ET.Element, by_path: bool, by_location: dict[str, str], track_ids: set[str]
) -> tuple[str, ...]:
    entries = node.findall("TRACK")
    # A truncated playlist would replace the stored one with fewer members,
    # so the count is checked before any key is resolved.
    _refuse_count_mismatch(None, f"playlist NODE Name={name!r}", node, len(entries))
    ids: list[str] = []
    for entry in entries:
        key = (entry.get("Key") or "").strip()
        if not key:
            raise ValueError(f"playlist NODE Name={name!r}: a TRACK has no Key")
        if by_path:
            if key not in by_location:
                raise ValueError(f"playlist NODE Name={name!r}: Location={key!r} matches no COLLECTION TRACK")
            ids.append(by_location[key])
        else:
            if "rbxml_" + key not in track_ids:
                raise ValueError(f"playlist NODE Name={name!r}: TrackID={key!r} matches no COLLECTION TRACK")
            ids.append("rbxml_" + key)
    return tuple(ids)


def _read_nodes(parent: ET.Element, by_location: dict[str, str], track_ids: set[str]) -> Iterator[Playlist]:
    seen_names: set[str] = set()
    for node in parent.findall("NODE"):
        name = node.get("Name") or ""
        if name in seen_names:
            # Playlists are keyed by folder path and name, so a second sibling of the
            # same name would replace the first one's members without a word.
            raise ValueError(f"NODE Name={name!r} appears twice under one folder; refusing to replace its members")
        seen_names.add(name)
        node_type = (node.get("Type") or "").strip()
        if node_type == "0":
            # Checked before descending so a short folder is named as such,
            # not reported as a problem in one of its children.
            _refuse_count_mismatch(None, f"folder NODE Name={name!r}", node, len(node.findall("NODE")), attr="Count")
            yield Playlist(name=name, track_ids=(), children=tuple(_read_nodes(node, by_location, track_ids)))
            continue
        if node_type != "1":
            raise ValueError(
                f"playlist NODE Name={name!r}: Type={node.get('Type')!r} is neither 0 (folder) nor 1 (playlist)"
            )
        # rekordbox reads a playlist with no KeyType as TrackID-keyed (0);
        # only an explicit value outside 0/1 is malformed.
        key_type = node.get("KeyType", "0").strip()
        if key_type not in ("0", "1"):
            raise ValueError(
                f"playlist NODE Name={name!r}: KeyType={node.get('KeyType')!r} is neither 0 (TrackID) nor 1 (Location)"
            )
        ids = _resolve_entries(name, node, key_type == "1", by_location, track_ids)
        yield Playlist(name=name, track_ids=ids)


def _float(raw: str | None, name: str) -> float | None:
    """An absent or blank attribute is missing; anything else must parse."""
    if raw is None or not raw.strip():
        return None
    # The rekordbox XML format allows a dot or comma decimal separator on
    # floating-point fields, so "128,5" is 128.5; "1,5,6" still fails below.
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not a number") from None
    if not math.isfinite(value):
        raise ValueError(f"{name}={raw!r} is not a finite number")
    return value


def _int(raw: str | None, name: str) -> int | None:
    """Like ``_float``, but a fractional value is malformed, never truncated.

    Parsed as a decimal, not a float, so a count past 2**53 keeps every digit.
    """
    if raw is None or not raw.strip():
        return None
    try:
        value = Decimal(raw.strip().replace(",", "."))
    except InvalidOperation:
        raise ValueError(f"{name}={raw!r} is not a number") from None
    if not value.is_finite():
        raise ValueError(f"{name}={raw!r} is not a finite number")
    # Magnitude first, read from the exponent: abs() and to_integral_value() apply
    # the decimal context, which overflows on an exponent such as 1e1000000.
    # Zero is exempt: 0e1000000 has a large adjusted exponent but is exactly zero.
    if not value.is_zero() and (
        value.copy_abs().adjusted() >= len(str(_STORE_INT_MAX)) or value.copy_abs() > _STORE_INT_MAX
    ):
        raise ValueError(f"{name}={raw!r} is too large to store")  # before int(), which would expand the digits
    if value != value.to_integral_value():
        raise ValueError(f"{name}={raw!r} is not a whole number")
    return int(value)


def _refuse_count_mismatch(source: Path | None, label: str, el: ET.Element, found: int, attr: str = "Entries") -> None:
    """``Entries`` (or a folder's ``Count``) is the number of children the element
    declares; a mismatch means the export was cut short or corrupted, so it stops the import."""
    raw = el.get(attr)
    if raw is None or not raw.strip():
        raise ValueError(f"{_where(source)}{label} has no {attr}")
    declared = _count(raw, attr)
    if declared != found:
        raise ValueError(f"{_where(source)}{label} {attr}={raw!r} but {found} children are present")


def _where(source: Path | None) -> str:
    return f"{source}: " if source is not None else ""


def _count(raw: str | None, name: str) -> int:
    value = _int(raw, name)
    if value is not None and value < 0:
        raise ValueError(f"{name}={raw!r} is negative")
    return value or 0


def _ms(seconds: str | None, name: str) -> int | None:
    """Seconds to milliseconds, scaled as an exact decimal: a float would round a value near the
    store's limit across it. Blank is missing; a negative time is malformed."""
    if seconds is None or not seconds.strip():
        return None
    try:
        value = Decimal(seconds.strip().replace(",", "."))
    except InvalidOperation:
        raise ValueError(f"{name}={seconds!r} is not a number") from None
    if not value.is_finite():
        raise ValueError(f"{name}={seconds!r} is not a finite number")
    if value < 0:
        raise ValueError(f"{name}={seconds!r} is negative")
    # Checked from the exponent first, so the scaling below cannot overflow the decimal context.
    if not value.is_zero() and value.adjusted() >= len(str(_STORE_INT_MAX)):
        raise ValueError(f"{name}={seconds!r} is too large to store")
    exact = Context(prec=len(seconds) + 8)
    ms = value.scaleb(3, context=exact).to_integral_value(rounding=ROUND_HALF_EVEN, context=exact)
    if ms > _STORE_INT_MAX:  # 9223372036854775.8 seconds rounds past the limit, so this is checked after rounding
        raise ValueError(f"{name}={seconds!r} is too large to store")
    return int(ms)


# The largest integer the store (SQLite) holds.
_STORE_INT_MAX = 2**63 - 1


def _stars(raw: str | None, name: str) -> int | None:
    value = _int(raw, name)
    if value is None:
        return None
    if value not in _RATINGS:
        raise ValueError(f"{name}={raw!r} is not a rekordbox rating (0, 51, 102, 153, 204 or 255)")
    return _RATINGS[value]


def _colour(raw: str | None, name: str) -> int | None:
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw, 16)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not a hex color") from None
    if not 0 <= value <= 0xFFFFFF:
        raise ValueError(f"{name}={raw!r} is not a 24-bit RGB color")
    return value
