"""``TraktorAdapter`` -- open-dj v0 Adapter Protocol implementation.

Reads a Traktor ``collection.nml`` into an ``OpenDjLibrary`` and writes the
same library back out. Runs against synthetic fixtures; no side effects on
the user's real Traktor install.
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from apps.adapters.traktor.capabilities import TRAKTOR_CAPABILITIES
from apps.adapters.traktor.mappers import (
    camelot_to_traktor_key,
    cue_color_from_type,
    opendj_cue_type_to_traktor,
    stars_to_traktor_rating,
    traktor_cue_type_to_opendj,
    traktor_key_to_camelot,
    traktor_rating_to_stars,
)
from apps.adapters.traktor.nml import NMLDocument, NMLEntry
from apps.open_dj import (
    AdapterReport,
    Capabilities,
    CuePoint,
    OpenDjLibrary,
    Playlist,
    Track,
)

# --------------------------------------------------------------- options


@dataclass
class TraktorAdapterOptions:
    """Runtime options for ``TraktorAdapter``.

    ``preserve_extended_data``: when True (default) any ``<EXTENDEDDATA>``
    child on an ``<ENTRY>`` is stored on the track's ``extensions`` under
    ``x_traktor_extended_data`` (base64). Set False to drop-with-warning.
    """

    preserve_extended_data: bool = True


# --------------------------------------------------------------- helpers


def _stable_track_id(file_path: str) -> str:
    """Per-adapter stable track ID (Phase 16, open-dj v0.1).

    Phase 5 ships ``apps.shared.state.ids.stable_id`` with a different
    shape: it returns ``(sha1_hex, tier)`` keyed on ISRC / fingerprint /
    path+mtime, which is the archival dedup key. This adapter instead
    needs a short vendor-tagged ID that is stable across round-trips
    through a single ``collection.nml`` (no mtime available at read
    time, no ISRC on many Traktor entries). We therefore keep a local
    SHA-1 over the file path with a ``traktor_`` prefix so IDs are
    visually disambiguated from the Serato adapter and from the
    archival stable_id. A cross-adapter helper is not planned; Phase 7
    dedup re-keys to the archival ID when better signals exist.
    """
    return "traktor_" + hashlib.sha1(file_path.encode("utf-8")).hexdigest()[:16]


def _split_artists(raw: str) -> tuple[str, ...]:
    if not raw:
        return ()
    # Split on common separators without being clever. Preserve raw on write.
    for sep in (" & ", "; ", " feat. ", " ft. "):
        if sep in raw:
            parts = [p.strip() for p in raw.split(sep) if p.strip()]
            return tuple(parts)
    return (raw.strip(),)


# ---------------------------------------------------------------- adapter


@dataclass
class TraktorAdapter:
    """Traktor (Native Instruments) adapter (open-dj v0.1)."""

    name: str = "traktor"
    options: TraktorAdapterOptions = field(default_factory=TraktorAdapterOptions)

    # ----------------------------------------------------------------- API

    def capabilities(self) -> Capabilities:
        return TRAKTOR_CAPABILITIES

    def read(self, source: Path) -> tuple[OpenDjLibrary, AdapterReport]:
        """Read ``source`` (a ``collection.nml`` file) into an OpenDjLibrary."""
        report = AdapterReport()
        doc = NMLDocument.read(source)
        tracks = tuple(self._read_entry(e, report) for e in doc.entries())
        playlists = tuple(self._read_playlists(doc, report))
        report.counts["tracks_read"] = len(tracks)
        report.counts["playlists_read"] = len(playlists)
        return (
            OpenDjLibrary(version="0.1", tracks=tracks, playlists=playlists),
            report,
        )

    def write(self, library: OpenDjLibrary, target: Path) -> AdapterReport:
        """Serialise ``library`` into a new ``collection.nml`` at ``target``."""
        report = AdapterReport()
        doc = NMLDocument.empty()
        for track in library.tracks:
            entry = doc.add_entry()
            self._write_entry(entry, track, report)
        # Resolve typed track_ids back to their source file_paths so
        # ``_write_playlists`` emits Traktor-native PRIMARYKEY entries
        # that survive a write/read round-trip (Codex P16-F02).
        id_to_path = {
            _stable_track_id(t.file_path): t.file_path for t in library.tracks
        }
        self._write_playlists(doc, library.playlists, id_to_path=id_to_path)
        doc.write(target)
        report.counts["tracks_written"] = len(library.tracks)
        report.counts["playlists_written"] = len(library.playlists)
        return report

    # ---------------------------------------------------------- entry read

    def _read_entry(self, entry: NMLEntry, report: AdapterReport) -> Track:
        path = entry.location_path() or "unknown"
        track_id = _stable_track_id(path)
        bpm_str = entry.get_subchild_attr("TEMPO", "BPM")
        try:
            bpm = float(bpm_str) if bpm_str else None
        except ValueError:
            bpm = None
            report.warn(field="bpm", track_id=track_id, action="dropped",
                        reason=f"unparseable TEMPO @BPM {bpm_str!r}")
        key_value = entry.get_subchild_attr("MUSICAL_KEY", "VALUE")
        key = traktor_key_to_camelot(key_value)
        info = entry.get_subchild("INFO")
        rating = None
        playtime = None
        if info is not None:
            rating = traktor_rating_to_stars(info.get("RANKING"))
            playtime_s = info.get("PLAYTIME")
            if playtime_s:
                try:
                    playtime = round(float(playtime_s) * 1000)
                except ValueError:
                    playtime = None
        cues = tuple(self._read_cues(entry, track_id, report))
        extensions: dict[str, object] = {}
        if self.options.preserve_extended_data:
            ext_el = entry.get_subchild("EXTENDEDDATA")
            if ext_el is not None and ext_el.text:
                extensions["x_traktor_extended_data"] = ext_el.text
        else:
            if entry.get_subchild("EXTENDEDDATA") is not None:
                report.warn(field="extended_data", track_id=track_id, action="dropped",
                            reason="preserve_extended_data=False")
        return Track(
            track_id=track_id,
            file_path=path,
            title=entry.title,
            artists=_split_artists(entry.artist),
            album=(entry.get_subchild_attr("ALBUM", "TITLE") or ""),
            bpm=bpm,
            key_camelot=key,
            rating=rating,
            duration_ms=playtime,
            cues=cues,
            extensions=extensions,
        )

    def _read_cues(
        self, entry: NMLEntry, track_id: str, report: AdapterReport
    ) -> list[CuePoint]:
        cues: list[CuePoint] = []
        for i, cue_el in enumerate(entry.element.findall("CUE_V2")):
            cue_type = traktor_cue_type_to_opendj(cue_el.get("TYPE"))
            start_ms = 0
            try:
                start_ms = round(float(cue_el.get("START", "0")))
            except ValueError:
                report.warn(field="cue_points.position", track_id=track_id,
                            action="dropped", reason="unparseable CUE_V2 @START")
            length_ms = None
            length_attr = cue_el.get("LEN")
            if length_attr:
                try:
                    length_ms = round(float(length_attr))
                except ValueError:
                    length_ms = None
            cues.append(
                CuePoint(
                    index=i,
                    position_ms=start_ms,
                    type=cue_type,
                    name=cue_el.get("NAME", ""),
                    color_rgb=cue_color_from_type(cue_type),
                    length_ms=length_ms,
                )
            )
        return cues

    # --------------------------------------------------------- entry write

    def _write_entry(
        self, entry: NMLEntry, track: Track, report: AdapterReport
    ) -> None:
        if track.title:
            entry.title = track.title
        artist_str = " & ".join(track.artists) if track.artists else ""
        if artist_str:
            entry.artist = artist_str
        # LOCATION -- split path into volume + directory + file.
        location = ET.SubElement(entry.element, "LOCATION")
        volume, directory, filename = _split_location(track.file_path)
        if volume:
            location.set("VOLUME", volume)
        if directory:
            location.set("DIR", directory)
        if filename:
            location.set("FILE", filename)
        if track.album:
            ET.SubElement(entry.element, "ALBUM", {"TITLE": track.album})
        info_attrs: dict[str, str] = {}
        rating_value = stars_to_traktor_rating(track.rating)
        if rating_value is not None:
            info_attrs["RANKING"] = str(rating_value)
        if track.duration_ms is not None:
            info_attrs["PLAYTIME"] = f"{track.duration_ms / 1000:.3f}"
        if info_attrs:
            ET.SubElement(entry.element, "INFO", info_attrs)
        if track.bpm is not None:
            ET.SubElement(entry.element, "TEMPO", {"BPM": f"{track.bpm:.3f}"})
        key_int = camelot_to_traktor_key(track.key_camelot)
        if key_int is not None:
            ET.SubElement(entry.element, "MUSICAL_KEY", {"VALUE": str(key_int)})
        # Cue points.
        for cue in track.cues:
            attrs = {
                "NAME": cue.name,
                "START": str(cue.position_ms),
                "TYPE": str(opendj_cue_type_to_traktor(cue.type)),
            }
            if cue.length_ms is not None:
                attrs["LEN"] = str(cue.length_ms)
            ET.SubElement(entry.element, "CUE_V2", attrs)
            if cue.color_rgb is not None and cue.color_rgb != cue_color_from_type(cue.type):
                report.warn(
                    field="cue_points.color",
                    track_id=track.track_id,
                    action="downgraded",
                    reason=f"Traktor paints cue type {cue.type!r} with fixed palette; user color discarded.",
                )
        if self.options.preserve_extended_data:
            ext = track.extensions.get("x_traktor_extended_data")
            if isinstance(ext, str) and ext:
                el = ET.SubElement(entry.element, "EXTENDEDDATA")
                el.text = ext

    # -------------------------------------------------------------- playlists

    def _read_playlists(
        self, doc: NMLDocument, _report: AdapterReport
    ) -> list[Playlist]:
        playlists: list[Playlist] = []
        pls_el = doc.root.find("PLAYLISTS")
        if pls_el is None:
            return playlists
        # Flatten Traktor's NODE tree. For v0 we skip folders (TYPE="FOLDER")
        # and promote each PLAYLIST node into a flat open-dj playlist.
        for node in pls_el.iter("NODE"):
            if node.get("TYPE") != "PLAYLIST":
                continue
            pl = node.find("PLAYLIST")
            if pl is None:
                continue
            track_ids: list[str] = []
            for e in pl.findall("ENTRY"):
                primary = e.find("PRIMARYKEY")
                if primary is None:
                    continue
                key = primary.get("KEY", "")
                # Traktor playlist keys are paths; reuse our stable-id helper.
                track_ids.append(_stable_track_id(key))
            playlists.append(
                Playlist(name=node.get("NAME", "unnamed"), track_ids=tuple(track_ids))
            )
        return playlists

    def _write_playlists(
        self, doc: NMLDocument, playlists: tuple[Playlist, ...],
        id_to_path: dict[str, str] | None = None,
    ) -> None:
        root_pls = doc.root.find("PLAYLISTS")
        if root_pls is None:
            root_pls = ET.SubElement(doc.root, "PLAYLISTS")
        root_node = ET.SubElement(
            root_pls, "NODE", {"TYPE": "FOLDER", "NAME": "$ROOT"}
        )
        subnodes = ET.SubElement(root_node, "SUBNODES", {"COUNT": str(len(playlists))})
        for pl in playlists:
            node = ET.SubElement(
                subnodes, "NODE", {"TYPE": "PLAYLIST", "NAME": pl.name}
            )
            playlist_el = ET.SubElement(
                node, "PLAYLIST", {"ENTRIES": str(len(pl.track_ids)), "TYPE": "LIST"}
            )
            for tid in pl.track_ids:
                entry_el = ET.SubElement(playlist_el, "ENTRY")
                # PRIMARYKEY must carry the Traktor-native reference (the
                # file path, which read() will re-hash through the same
                # ``_stable_track_id`` helper). Writing the already-hashed
                # typed ``track_id`` here means read() would hash the
                # hash, permanently breaking playlist membership on
                # round-trip (Codex P16-F02). Fall back to the raw id
                # only for orphan references so we never write an empty
                # PRIMARYKEY.
                key = (id_to_path or {}).get(tid, tid)
                ET.SubElement(
                    entry_el, "PRIMARYKEY", {"KEY": key, "TYPE": "TRACK"}
                )


def _split_location(file_path: str) -> tuple[str, str, str]:
    """Return (volume, directory, filename) in Traktor's scheme.

    Traktor's NML stores ``VOLUME`` (e.g. "Macintosh HD") separately from
    ``DIR`` (e.g. ``/:Users/:alice/:Music/:``). For v0 we don't try to
    reverse-engineer the original volume; we use an empty volume and a
    trailing-slash-colon directory. Good enough for synthetic fixtures.
    """
    if not file_path:
        return ("", "", "")
    # PurePosixPath, never Path: on Windows ``Path("/a/b").parent`` comes
    # back with backslashes, desyncing the write-side DIR encoding from the
    # read-side reconstruction and breaking the stable-id round-trip.
    # open-dj file_path strings are posix-style by contract; normalise any
    # stray backslashes first so both platforms hash identical locations.
    p = PurePosixPath(file_path.replace("\\", "/"))
    parent = str(p.parent)
    name = p.name
    if parent and parent != ".":
        # Normalise forward slashes + trailing "/" into Traktor ":" style.
        dir_str = "/:" + parent.strip("/").replace("/", "/:") + "/:"
        return ("", dir_str, name)
    return ("", "/:", name)
