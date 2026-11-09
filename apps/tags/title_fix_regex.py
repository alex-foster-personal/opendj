"""Title-fix regex proposer and cover detection.

Split out of `apps/tags/title_fix.py` (#9686); the mini-PRD for these rules lives in that module's docstring.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from .title_fix_model import CFG, Proposal, Track
from .title_fix_provenance import featured_artists, provenance_tags
from .title_fix_vocab import (
    VERSION_TAGS,
    Rx,
    version_tags,
)

# ----------------------------------------------------------------- regex


def _sub_named(
    text: str,
    name: str,
    applied: list[str] | None,
    pattern: re.Pattern[str],
    repl: str | Callable[[re.Match[str]], str] = " ",
) -> str:
    new = pattern.sub(repl, text)
    if applied is not None and new != text:
        applied.append(name)
    return new


def _junk_unless_version(match: re.Match[str]) -> str:
    """A junk match is dropped, unless it names a version or a cover ("(Official Acoustic)", "(320kbps Remix)")."""
    return match.group() if _names_a_version(match.group()) else " "


def _names_a_version(text: str) -> bool:
    return bool(Rx.VERSION_WORD.search(text)) or any(pattern.search(text) for _, pattern in VERSION_TAGS)


def _strip_junk(text: str, applied: list[str] | None = None) -> str:
    """File junk removal. Each step that changes the text appends its name to `applied` (SAFE_TRANSFORMS)."""
    before = text
    text = text.translate(Rx.FULL_WIDTH)
    if applied is not None and text != before:
        applied.append("full-width")
    text = _sub_named(text, "cue-time", applied, Rx.CUE_TIME, "")
    text = _sub_named(text, "download-tail", applied, Rx.DOWNLOAD_TAIL, "")
    text = _sub_named(text, "promo-stars", applied, Rx.PROMO_STARS)
    text = _sub_named(text.strip(), "site-prefix", applied, Rx.SITE, "")
    text = _sub_named(text, "bitrate-bracket", applied, Rx.BITRATE_BRACKET, _junk_unless_version)
    text = _sub_named(text, "capture-suffix", applied, Rx.CAPTURE_SUFFIX)
    text = _sub_named(text, "pn-suffix", applied, Rx.PN_SUFFIX)
    text = _sub_named(text, "junk", applied, Rx.JUNK, _junk_unless_version)
    text = _sub_named(text, "site-mark", applied, Rx.SITE_MARK)
    text = _sub_named(text, "copy-suffix", applied, Rx.COPY_SUFFIX, "")
    if "\\'" in text:
        text = text.replace("\\'", "'")
        if applied is not None:
            applied.append("escaped-quote")
    if "_" in text and " " not in text.strip("_"):
        text = Rx.UNDERSCORE_OUTSIDE_KNOWN.sub(lambda m: m.group() if m.group() != "_" else " ", text)
        if applied is not None:
            applied.append("underscores")
    tidy = re.sub(r"\s{2,}", " ", text).strip(" -")
    if applied is not None and tidy != text.strip():
        applied.append("whitespace")
    return tidy


def _split_artist(text: str) -> tuple[str, str] | None:
    """Spaced "Artist - Title". A version-only right side ("Slow Harbor - 2003 Remastered") is no split."""
    parts = Rx.ARTIST_SPLIT.split(text, maxsplit=1)
    if len(parts) != 2 or Rx.VERSION_ONLY.fullmatch(parts[1]):
        return None
    return parts[0].strip(), parts[1].strip()


def _split_unspaced(text: str) -> tuple[str, str] | None:
    """Splits "Gloam-404-All The Quiet Things" when no spaced separator exists. Conservative: the left
    part must look like an artist (several words, a digit or its own hyphen, no bracket) and the
    right part must be several words, so "Re-Wander" or "Folk-Pop Is Calm" never split."""
    if Rx.ARTIST_SPLIT.search(text):
        return None
    for m in Rx.UNSPACED_DASH.finditer(text):
        left, right = text[: m.start()].strip(), text[m.end() :].strip()
        if (
            len(left) >= 3
            and re.search(r"[\s\d-]", left)
            and not re.search(r"[(\[]", left)
            and " " in right
            and not Rx.VERSION_ONLY.fullmatch(right)
        ):
            return left, right
    return None


def _strip_track_no(title: str, artist_filled: bool) -> str:
    if m := Rx.TRACK_NO_PADDED.match(title):
        return title[m.end() :]
    if (m := Rx.TRACK_NO_BARE.match(title)) and (artist_filled or _split_artist(title[m.end() :])):
        return title[m.end() :]
    return title


def _fix_mojibake(text: str) -> str:
    """Re-decodes UTF-8 that was read as cp1252/latin-1 ("Der B\u00c3\u00a4r" -> "Der B\u00e4r"). Only
    when the bytes decode cleanly as UTF-8 with no replacement character; a real "\u00c3me" never matches."""
    if not Rx.MOJIBAKE.search(text):
        return text
    for codec in ("cp1252", "latin-1"):
        try:
            fixed = text.encode(codec).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if "\ufffd" not in fixed and fixed != text:
            return fixed
    return text


def _undo_windows_substitution(title: str) -> str:
    """Known "_" stand-ins for Windows-illegal characters ("AB_CD") are undone. Any other underscore left
    in the final title goes to review (regex_propose); a real name like "u_v" in the artist is never touched."""
    for bad, good in Rx.WINDOWS_KNOWN:
        title = title.replace(bad, good)
    return title


def _filename_title(track: Track) -> str:
    stem = Rx.COPY_TAIL_NUMBER.sub("", _strip_junk(Rx.EXT.sub("", track.filename)))
    if m := Rx.KEY_BPM_SUFFIX.search(stem):
        stem = stem[: m.start()]
    if m := Rx.CAMELOT_BPM_SUFFIX.search(stem):
        stem = stem[: m.start()]
    return _strip_track_no(stem, True).strip()


def _id3v1_extension(track: Track, core: str, title: str) -> str:
    """An exactly-30-char title (ID3v1 cap) whose filename title starts with it and runs longer: returns
    the filename title (for review only, never auto-applied: most 30-char titles are coincidence)."""
    if len(core) != 30 or not track.filename:
        return ""
    longer = _filename_title(track)
    return longer if len(longer) > len(title) and longer.startswith(title) else ""


def _strip_vs_tail(title: str, artist: str) -> str:
    """Drops "- A Vs B" from the end of the title when every name in it is already credited."""
    m = Rx.VS_TAIL.search(title)
    if not m or not artist:
        return title
    credited = {_fold(n) for n in _credited(artist)}
    names = [_fold(n) for n in Rx.VS_SPLIT.split(m.group("tail"))]
    return title[: m.start()] if all(n in credited for n in names) else title


@dataclass
class _Draft:
    """regex_propose's working state: every edit records its name in `applied` (see SAFE_TRANSFORMS)."""

    title: str
    artist: str
    applied: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    key: str = ""
    energy: str = ""
    bpm: str = ""
    producer: str = ""

    def edit(self, name: str, title: str) -> None:
        if title != self.title:
            self.title = title
            self.applied.append(name)


def _strip_mik(draft: _Draft) -> None:
    draft.edit("mojibake", _fix_mojibake(draft.title))
    if not CFG.STRIP_MIK_PREFIX:
        return
    if m := Rx.MIK_PAREN.match(draft.title):
        title = m.group(3)
        if title.count("(") > title.count(")"):  # "7A (7 - Custard 20 (Remy Calder Remix)": MIK ate a ")"
            title += ")"
        draft.key, draft.energy = m.group(1), m.group(2)
        draft.edit("mik-paren", title)
    elif m := Rx.MIK_PREFIX.match(draft.title):
        # A lone "3 - " is MIK energy only with a key before it or a filled artist field;
        # otherwise it is a track number ("3 - Vantor Quill - Glasswork") and still stripped.
        draft.key = m.group(1) or ""
        draft.energy = m.group(2) if draft.key or draft.artist else ""
        draft.edit("mik-prefix" if draft.energy else "track-number", draft.title[m.end() :])


def _strip_file_junk(draft: _Draft, track: Track) -> None:
    draft.edit("title-extension", Rx.EXT.sub("", draft.title))
    if m := Rx.KEY_BPM_SUFFIX.search(draft.title):
        draft.key, draft.bpm = draft.key or m.group(1), m.group(2)
        draft.edit("key-bpm-suffix", draft.title[: m.start()])
    draft.title = _strip_junk(draft.title, draft.applied)
    if m := Rx.BEATPORT_ID.match(draft.title):  # Beatport files: "<id>_Title_(Mix)" uses "_" for spaces
        draft.edit("beatport-id", draft.title[m.end() :].replace("_", " "))
    draft.edit("track-number", _strip_track_no(draft.title, bool(draft.artist)).strip())
    if not draft.title and track.filename:
        draft.edit("filename-title", _strip_junk(Rx.EXT.sub("", track.filename)))
    if m := Rx.PRODUCER.search(draft.title):
        draft.producer = m.group("who").strip()
        draft.edit("producer-credit", draft.title[: m.start()].strip())
    draft.edit("windows-illegal", _undo_windows_substitution(draft.title))


def _assign_artist(draft: _Draft, track: Track) -> Cover | None:
    if draft.artist and (cleaned := _clean_junk_artist(draft.artist, draft.title)) is not None:
        draft.artist = cleaned
        draft.applied.append("junk-artist")
        draft.reasons.append(f"artist field {track.artist.strip()!r} holds the title or a site name")
    cover = _detect_cover(draft.title, draft.artist)
    if cover:
        draft.artist, draft.title = cover.artist, cover.title
        draft.applied.append("cover")
        draft.reasons.append(f"cover ({cover.note}): review-only until precision is measured")
    draft.edit("vs-tail", _strip_vs_tail(draft.title, draft.artist).strip())
    if not draft.artist and (split := _split_artist(draft.title) or _split_unspaced(draft.title)):
        draft.artist = split[0]
        draft.edit("artist-split", split[1])
    elif draft.artist and draft.title.lower().startswith(draft.artist.lower() + " - "):
        draft.edit("artist-dup", draft.title[len(draft.artist) + 3 :])
    draft.title = draft.title.strip()
    return cover


def regex_propose(track: Track) -> Proposal:
    draft = _Draft(track.title.strip(), track.artist.strip())
    _strip_mik(draft)
    core, mik_analyzed = draft.title, bool(draft.energy)
    _strip_file_junk(draft, track)
    if longer := _id3v1_extension(track, core, draft.title):
        draft.edit("id3v1-filename", longer)
        draft.reasons.append("title is exactly 30 characters (ID3v1 cap) and the filename continues it")
    cover = _assign_artist(draft, track)
    if "_" in draft.title:
        draft.reasons.append(f"underscore in {draft.title!r} may stand for a Windows-illegal character")
    tags = _merge_tags(version_tags(draft.title), cover.tags if cover else (), provenance_tags(track, mik_analyzed))
    return Proposal(
        artist=draft.artist,
        title=draft.title,
        key=draft.key,
        energy=draft.energy,
        bpm=draft.bpm,
        tags=tags,
        original_artist=cover.original_artist if cover else "",
        review_reasons=tuple(draft.reasons),
        mik_analyzed=mik_analyzed,
        featured_artists=featured_artists(draft.title, track.artist),
        producer=draft.producer,
        transforms=tuple(dict.fromkeys(draft.applied)),
    )


def _merge_tags(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(tag for group in groups for tag in group))


def _clean_junk_artist(artist: str, title: str) -> str | None:
    """The artist field is a copy of "Artist - Title" or carries a site watermark: returns the
    cleaned artist (maybe ""), else None. A real credit ("Tallow,Marrow") is never touched."""
    stripped = _strip_junk(artist)
    has_site = bool(Rx.SITE_MARK.search(artist))
    split = _split_artist(stripped)
    song = _fold(split[1]) if split else ""
    copies_title = (len(song) >= 3 and _fold(title).endswith(song)) or (
        _fold(stripped) == _fold(title) and bool(_split_artist(title))
    )
    if not (has_site or copies_title):
        return None
    if split:
        return split[0]
    if copies_title and (title_split := _split_artist(title)):
        return title_split[0]
    return stripped


# ----------------------------------------------------------------- covers


class Cov:
    INSTRUMENT = re.compile(
        r"guitar|acoustic|piano|organ|violin|cello|ukulele|drums?|bass|sax(?:ophone)?|harp|strings?|orchestral"
        r"|metal|punk|rock|jazz|lo-?fi|synth|marimba|flute|trumpet|vocal|a ?cappella|acapella",
        re.I,
    )
    KARAOKE = re.compile(r"\bkaraoke\b", re.I)
    # "X - guitar cover by Y", "X (covered by Y)": Y performs.
    COVER_BY = re.compile(
        r"^(?P<head>.+?)(?:\s*-\s*|\s*[(\[]\s*|\s+)(?:(?P<kind>[A-Za-z][\w-]*)\s+)?cover(?:ed)? by\s+"
        r"(?P<perf>[^()\[\]]+?)\s*[)\]]?\s*$",
        re.I,
    )
    # Bracketed only: "Under Cover of Fog" is a song title.
    ORIGINAL_CREDIT = re.compile(
        r"\s*[(\[]\s*(?P<how>cover of|made famous by|originally performed by|in the style of)\s+"
        r"(?P<orig>[^()\[\]]+?)\s*[)\]]",
        re.I,
    )
    NAMED_COVER = re.compile(r"(?:\s+-\s+|\s*[(\[]\s*)(?P<who>[^()\[\]]+?)\s+cover\s*[)\]]?\s*$", re.I)
    BARE_COVER = re.compile(r"[(\[]\s*cover\s*[)\]]\s*$", re.I)
    PERFORMER_VERSION = re.compile(
        r"(?:\s*\((?P<orig>[^()]+)\))?\s*(?:\s-\s|[(\[])\s*(?P<who>[^()\[\]]+?)\s+version\s*[)\]]?\s*$", re.I
    )
    QUOTED_SONG = re.compile(r"^(?P<orig>.+?)\s+[\"\u201c](?P<song>[^\"\u201d]+)[\"\u201d]\s*$")


@dataclass(frozen=True)
class Cover:
    artist: str
    title: str
    original_artist: str
    tags: tuple[str, ...]
    note: str


def _credited(artist: str) -> list[str]:
    return [n.strip() for n in re.split(r"\s*(?:,|&|\bfeat\.?|\bft\.?)\s*", artist, flags=re.I) if n.strip()]


def _instrument_tags(kind: str) -> tuple[str, ...]:
    word = kind.strip().lower()
    return (f"{re.sub(r'[^a-z]+', '-', word).strip('-')}-cover",) if Cov.INSTRUMENT.fullmatch(word) else ()


def _song_and_original(head: str) -> tuple[str, str]:
    if m := Cov.QUOTED_SONG.match(head):
        return m.group("song").strip(), m.group("orig").strip()
    if split := _split_artist(head):
        return split[1], split[0]
    return head.strip().strip('"'), ""


def _named_cover(title: str, artist: str) -> Cover | None:
    """ "- Fernwick Cover" names a credited performer; "- Organ Cover" names an instrument."""
    m = Cov.NAMED_COVER.search(title)
    if not m:
        return None
    names = _credited(artist)
    who = m.group("who").strip()
    instrument = _instrument_tags(who)
    performer = next((n for n in names if n.casefold() == who.casefold()), "")
    if not (instrument or performer):
        return None
    note = "named performer" if performer else "instrument cover, performer guessed as first credited"
    performer = performer or (names[0] if names else "")
    others = [n for n in names if n != performer]
    return Cover(performer if others else artist, title, ",".join(others), ("cover", *instrument), note)


def _detect_cover(title: str, artist: str) -> Cover | None:
    """Explicit cover wording only. "Tribute", "Recovery" and "Undercover" are never covers."""
    credit = Cov.ORIGINAL_CREDIT.search(title)
    original = credit.group("orig").strip() if credit else ""
    if Cov.KARAOKE.search(title):
        return Cover(artist, title, original, ("karaoke", "cover"), "karaoke")
    if m := Cov.COVER_BY.match(title):
        return _cover_by(m, artist, original)
    if credit:
        rest = re.sub(r"\s{2,}", " ", title[: credit.start()] + title[credit.end() :]).strip()
        return Cover(artist, f"{rest} (Cover)", original, ("cover",), credit.group("how").lower())
    if named := _named_cover(title, artist):
        return named
    if Cov.BARE_COVER.search(title):
        return Cover(artist, title, "", ("cover",), "(Cover)")
    return _performer_version(title, artist)


def _cover_by(m: re.Match[str], artist: str, original: str) -> Cover:
    """ "X - guitar cover by Y": a filled artist field wins over Y; the head may carry the original artist."""
    song, head_original = _song_and_original(m.group("head"))
    instrument = _instrument_tags(m.group("kind") or "")
    marker = f"({m.group('kind').strip().title()} Cover)" if instrument else "(Cover)"
    performer = artist or m.group("perf").strip()
    return Cover(performer, f"{song} {marker}", head_original or original, ("cover", *instrument), "cover by")


def _performer_version(title: str, artist: str) -> Cover | None:
    """ "Song (Me Version)" is a cover only when "Me" is a credited artist."""
    m = Cov.PERFORMER_VERSION.search(title)
    if not (m and any(n.casefold() == m.group("who").strip().casefold() for n in _credited(artist))):
        return None
    orig = (m.group("orig") or "").strip()
    new_title = re.sub(r"\s{2,}", " ", title.replace(f"({orig})", "", 1)).strip() if orig else title
    return Cover(artist, new_title, orig, ("cover",), "performer version")


def _fold(text: str) -> str:
    return re.sub(r"\W+", "", text).casefold()
