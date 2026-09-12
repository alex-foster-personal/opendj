"""Load the crate as Track records: rekordbox XML metadata + local-audio resolution.

Audio presence is resolved by BASENAME against an index of the local audio
mirrors -- a pragmatic stand-in until the library-normalization thread lands
proper relinking (specs/library-normalization.md). The resolution table is
persisted so the enrichment thread can reuse it as relink candidates.
"""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

from apps.lyrics.sources.base import STATE_DIR, Track

XML_PATH = Path.home() / "Music" / "rekordbox-air-export" / "rekordbox-export-2024-11-26.xml"
AUDIO_ROOTS = (Path.home() / "Music" / "air-library", Path.home() / "Music" / "Music")
AUDIO_EXTS = frozenset({".mp3", ".m4a", ".aiff", ".aif", ".wav", ".flac", ".ogg"})

#-----------------------------------------------------------------------------


def normalize_title(title: str) -> str:
    """Strip up to TWO leading dash-separated short prefixes (MIK-style Camelot
    key and/or energy number, e.g. '9A - 7 - Gabriel - Soulwax Remix').
    Deliberately never strips version/remix suffixes -- they change which
    lyrics are correct. Full structured normalization is the Haiku pass
    (specs/library-normalization.md capability B); this is the regex floor."""
    return re.sub(r"^(?:\d{1,2}[AB]?\s*-\s*){1,2}", "", title, flags=re.IGNORECASE).strip()


def primary_artist(artist: str) -> str:
    return re.split(
        r"\s*[,;/]\s*|\s+feat\.?\s+|\s+ft\.?\s+|\s+x\s+", artist, flags=re.IGNORECASE
    )[0].strip()


def collapse_duplicates(tracks: list[Track]) -> list[Track]:
    """Playlist copies share a key but differ in Location; prefer the copy
    whose audio resolved -- one broken path must not hide a working one
    (regression: an earlier first-write-wins collapse dropped ~30 resolved
    tracks, Fri 28 Aug 2026)."""
    unique: dict[str, Track] = {}
    for t in tracks:
        prev = unique.get(t.key)
        if prev is None or (prev.audio_path is None and t.audio_path is not None):
            unique[t.key] = t
    return list(unique.values())


def _build_audio_index() -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = defaultdict(list)
    for root in AUDIO_ROOTS:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if p.suffix.lower() in AUDIO_EXTS and p.is_file():
                index[p.name].append(p)
    if not index:
        raise SystemExit(f"[ERROR] no audio found under {[str(r) for r in AUDIO_ROOTS]}")
    return index


def load_tracks() -> list[Track]:
    if not XML_PATH.is_file():
        raise SystemExit(f"[ERROR] rekordbox export missing: {XML_PATH}")
    index = _build_audio_index()
    tracks: list[Track] = []
    for tr in ET.parse(XML_PATH).getroot().findall(".//TRACK[@Location]"):
        artist, title, total = tr.get("Artist"), tr.get("Name"), tr.get("TotalTime")
        if not artist or not title or not total:
            continue
        basename = Path(urllib.parse.unquote(tr.get("Location", ""))).name
        hits = index.get(basename, [])
        tracks.append(Track(
            artist=artist,
            title=title,
            norm_title=normalize_title(title),
            primary_artist=primary_artist(artist),
            duration_s=int(float(total)),
            audio_path=str(hits[0]) if hits else None,
        ))
    if not tracks:
        raise SystemExit("[ERROR] zero usable tracks in the export")
    _persist_resolution(tracks)
    return tracks


def _persist_resolution(tracks: list[Track]) -> None:
    out = STATE_DIR / "source-availability" / "audio-resolution.tsv"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = ["key\tresolved\taudio_path\tartist\ttitle"]
    lines += [
        f"{t.key}\t{int(t.audio_path is not None)}\t{t.audio_path or ''}\t{t.artist}\t{t.title}"
        for t in tracks
    ]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
