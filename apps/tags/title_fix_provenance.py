"""Title-fix provenance tags and featured-artist credits.

Split out of `apps/tags/title_fix.py` (#9686); the mini-PRD for these rules lives in that module's docstring.
"""

from __future__ import annotations

import re

from .title_fix_model import CFG, Track

# ----------------------------------------------------------------- provenance tags and credits


class Prov:
    LOSSY = (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".mp4", ".wma")
    STREAMING = re.compile(r"^(?:spotify|tidal|soundcloud|beatport|beatsource|deezer|apple):", re.I)
    YOUTUBE = re.compile(
        # A converter marker only: a bare "(320kbps)" is not one (round 7, both judges; 28 library rows).
        r"\b(?:yt2mp3|y2mate|ytmp3|onlymp3|snapsave|savefrom)\b"
        r"|-[A-Za-z0-9_-]{11}-\d{2,3}k-\d{10,13}|-\s*youtube\s*$",
        re.I,
    )
    BEATPORT = re.compile(r"^\d{6,}_|_pn\.\w+$", re.I)
    COPY = re.compile(r"\(\d\)\.\w+$")
    VIDEO = re.compile(
        r"\b(?:(?:official|lyrics?)\s+(?:music\s+)?video|music video|visuali[sz]er)\b|[(\[]video[)\]]"
        r"|[(\[][^()\[\]]*\b(?:lyrics?|translation)\b[^()\[\]]*[)\]]",  # kept lyric/translation notes (round 7)
        re.I,
    )
    LIVE_WORDS = re.compile(r"\blive\b|@|\bset\b|boiler room|\bcercle\b", re.I)
    FEAT_TITLE = re.compile(
        r"[(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s+(?P<b>[^()\[\]]+?)\s*[)\]]"
        r"|\s(?:feat\.|ft\.|featuring)\s+(?P<u>[^()\[\]]+?)(?=\s+-\s|\s*[(\[]|$)",
        re.I,
    )
    FEAT_ARTIST = re.compile(r"\s(?:feat\.?|ft\.?|featuring)\s+(?P<u>.+)$", re.I)
    NAME_SPLIT = re.compile(r"\s*(?:,|&|\band\b)\s*", re.I)


def provenance_tags(track: Track, mik_analyzed: bool) -> tuple[str, ...]:
    """Row-level tags from the raw row (title, filename, folder, bitrate, length). Unknown values never tag."""
    raw = f"{track.title} | {track.filename}"
    lossy = track.filename.lower().endswith(Prov.LOSSY)
    long_rec = track.length_s is not None and track.length_s > CFG.LONG_RECORDING_S and not track.artist.strip()
    checks = (
        ("mik-analyzed", mik_analyzed),
        ("low-bitrate", lossy and track.bitrate is not None and 0 < track.bitrate <= CFG.LOW_BITRATE_KBPS),
        ("streaming-only", bool(Prov.STREAMING.match(track.folder) or Prov.STREAMING.match(track.filename))),
        ("youtube-rip", bool(Prov.YOUTUBE.search(track.title) or Prov.YOUTUBE.search(track.filename))),
        ("beatport-sourced", bool(Prov.BEATPORT.search(track.filename))),
        ("duplicate-copy", bool(Prov.COPY.search(track.filename))),
        ("video-source", bool(Prov.VIDEO.search(raw))),
        ("live-set", long_rec and bool(Prov.LIVE_WORDS.search(track.title))),
        ("dj-mix", long_rec and not Prov.LIVE_WORDS.search(track.title)),
    )
    return tuple(tag for tag, fired in checks if fired)


def featured_artists(title: str, artist_field: str) -> tuple[str, ...]:
    """feat./ft./featuring/(with X) credits from the title and the artist field. The title keeps its text."""
    found = [m.group("b") or m.group("u") for m in Prov.FEAT_TITLE.finditer(title)]
    if m := Prov.FEAT_ARTIST.search(artist_field):
        found.append(m.group("u"))
    unique: dict[str, str] = {}
    for chunk in found:
        for name in Prov.NAME_SPLIT.split(chunk):
            if name.strip(" ."):
                unique.setdefault(name.strip(" .").casefold(), name.strip(" ."))
    return tuple(unique.values())
