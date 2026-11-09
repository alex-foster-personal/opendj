"""Title-fix rule vocabulary: the Rx regex table and the version-tag vocabulary.

Split out of `apps/tags/title_fix.py` (#9686); the mini-PRD for these rules lives in that module's docstring.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


class Rx:
    SITE = re.compile(r"^\s*(?:\[[^\]]*(?:mp3|\.info|\.to)[^\]]*\]|\S+\.(?:to|info|com))\s*-\s*", re.I)
    JUNK = re.compile(
        r"\s*(?:[\[(](?![^\])]*(?:lyric|translat))[^\])]*(?:official|\bhd\b|visuali[sz]er|\bncs release|monstercat release"
        r"|free download|\bout now)[^\])]*[\])]"
        r"|-\s*official (?:music )?(?:video|audio)|-\s*youtube\s*$|www\.\S+"
        r"|\b(?:y2mate|onlymp3|yt2mp3|ytmp3|snapsave|savefrom)\.(?:is|com|to|info|net|io|app)\b)\s*",
        re.I,
    )
    BITRATE_BRACKET = re.compile(r"\s*[\[(][^\])]*\d+\s*kbps[^\])]*[\])]\s*", re.I)
    CAPTURE_SUFFIX = re.compile(r"\s*_capture\s*", re.I)
    PN_SUFFIX = re.compile(r"\s*_pn\s*", re.I)
    # SPDX-SnippetBegin
    # SPDX-License-Identifier: MIT
    # SPDX-SnippetCopyrightText: Copyright (c) 2020-2021 Web Scrobbler Team (web-scrobbler/metadata-filter)
    # YouTube rules ported from metadata-filter (see NOTICE): "**NEW**" promo markers, full-width brackets.
    PROMO_STARS = re.compile(r"\*+\s?[\w ]{1,20}?\s?\*+")
    FULL_WIDTH = str.maketrans({"\uff08": "(", "\uff09": ")", "\u3010": "[", "\u3011": "]"})
    # SPDX-SnippetEnd
    CUE_TIME = re.compile(r"^\s*\[\d{1,2}:\d{2}(?::\d{2})?\]\s*")
    # Y2Mate / onlymp3 download tail: -<11-char YouTube id>-<bitrate>k-<epoch ms>
    DOWNLOAD_TAIL = re.compile(r"\s*-\s*[A-Za-z0-9_-]{11}-\d{2,3}k-\d{10,13}\s*$")
    COPY_SUFFIX = re.compile(r"\s*\(\d\)\s*$")
    CAMELOT = r"(?:0?[1-9]|1[0-2])[AB]"  # Camelot keys run 1A-12B, optionally zero-padded
    MIK_PREFIX = re.compile(rf"^(?:({CAMELOT})\s*-\s*)?([1-9])\s+-\s+(?=\S)")
    MIK_PAREN = re.compile(rf"^({CAMELOT})\s+\(([1-9])\s+-\s+(.+)\)\s*$")
    KEY_BPM_SUFFIX = re.compile(r"\s+-\s+([A-G][b#]?m?)\s+-\s+(\d{2,3})\s*$")
    # Zero-padded ("01 Aliens", "023 - Woods") is always a track number; "99 Pebbles" is a title.
    TRACK_NO_PADDED = re.compile(r"^(?:0\d{1,2}\s*(?:-\s+|\.\s*|\s+))(?=\D)|^\d{6,}_")
    # Bare "12 - X" / "12. X" is a track number only when more evidence follows (see _strip_track_no):
    # "404 - Cinder" is the band 404.
    TRACK_NO_BARE = re.compile(r"^\d{2,3}\s*(?:-\s+|\.\s+)(?=\D)")
    ARTIST_SPLIT = re.compile(r"\s+[-\u2013\u2022]\s+")
    UNSPACED_DASH = re.compile(r"(?<=\w)-(?=[A-Z])")
    # The words that name a version; VERSION_ONLY and the junk strip's version protection share this one list.
    VERSION_WORDS = (
        r"original|extended|radio|club|dub|vip|instrumental|a ?cappella|acapella|live"
        r"|remaster(?:ed)?|digital|mix|edit|version|remix|mono|stereo|single|album|explicit|clean|acoustic|demo"
        r"|bootleg|sped up|slowed|reverb"
    )
    VERSION_WORD = re.compile(rf"\b(?:{VERSION_WORDS}|cover|karaoke)\b", re.I)  # a cover marker is identity too
    VERSION_ONLY = re.compile(rf"[\s()\[\]&+/,-]*(?:(?:\d{{4}}|{VERSION_WORDS})\b[\s()\[\]&+/,-]*)+", re.I)
    PLACEHOLDER_ARTIST = re.compile(r"\s*(?:unknown(?: artist)?|various(?: artists)?|n/?a|-)?\s*", re.I)
    EXT = re.compile(r"\.(?:mp3|wav|aiff?|flac|m4a|ogg)$", re.I)
    # A download-site watermark glued into a field: "DANCEDJ.CLUB", "Sub.FM".
    SITE_MARK = re.compile(
        r"\s*(?<![\w.])[A-Za-z0-9-]{3,}\.(?:club|com|net|org|ru|info|biz|cc|xyz|top|online|site|fm|io)(?![\w.])\s*",
        re.I,
    )
    # Producer credit: "- prod. X", "(prod. by X)", "[produced by X]" at the end of the title.
    PRODUCER = re.compile(
        r"\s*(?:-\s*|[(\[]\s*)(?:prod\.\s*(?:by\s+)?|prod(?:uced)?\s+by\s+)(?P<who>[^()\[\]]+?)\s*[)\]]?\s*$", re.I
    )
    # "75, Harbor Street (Vocal Radio Mix) - Nora Fenwick Vs Pat-Ridge": a trailing artist-vs-artist tail.
    VS_TAIL = re.compile(r"\s+-\s+(?P<tail>(?:(?!\s-\s)[^()\[\]])+?\svs\.?\s(?:(?!\s-\s)[^()\[\]])+?)\s*$", re.I)
    VS_SPLIT = re.compile(r"\s+vs\.?\s+", re.I)
    # UTF-8 bytes read as cp1252/latin-1: a lead byte char (U+00C2-U+00F4) then a continuation (U+0080-U+00BF).
    MOJIBAKE = re.compile(
        r"[\u00c2-\u00f4][\u0080-\u00bf\u2018-\u201e\u2020-\u2022\u2026\u2030\u2039\u203a\u20ac\u2122]"
    )
    # Characters Windows forbids in filenames, replaced by "_" on the way into tags. Known names only.
    # Bracketed text an LLM-confirmed answer may drop (R21): video/audio/quality junk, download promos, a MIK
    # "(6 - " wrapper, a copy number, a producer credit (moved to Proposal.producer). Anything else is content.
    BRACKET = re.compile(r"[(\[][^()\[\]]*[)\]]?")
    STRIPPABLE_BRACKET = re.compile(
        r"official|video|audio|\bhd\b|\bhq\b|visuali[sz]er|kbps|free download|out now|ncs release|monstercat release"
        r"|^[(\[]\s*[1-9]\s+-\s|^[(\[]\s*\d\s*[)\]]$|\bprod(?:\.|uced by)",
        re.I,
    )
    WINDOWS_KNOWN = (("AC_DC", "AC/DC"), ("_'", "'"))
    # Underscores become spaces, except inside a known Windows placeholder that a later step restores.
    UNDERSCORE_OUTSIDE_KNOWN = re.compile("|".join([*(re.escape(bad) for bad, _ in WINDOWS_KNOWN), "_"]))
    BEATPORT_ID = re.compile(r"^\d{6,}_")
    CAMELOT_BPM_SUFFIX = re.compile(r"\s+-\s+\d{1,2}[AB]\s+-\s+\d{2,3}\s*$")
    COPY_TAIL_NUMBER = re.compile(r"\s+\d\s*$")  # "Track 2.mp3": a second copy, not a title word
    MASHUP_JOIN = re.compile(r"\S\s+(?:[xX\u00d7]|vs\.?)\s+\S", re.I)
    # ALL CAPS goes to review only with these junk/credit words (Fri 2 Oct taxonomy: 68% of caps were styling).
    CAPS_JUNK = re.compile(
        r"\b(?:official|video|audio|lyrics?|feat|ft|featuring|deluxe|edition|remaster(?:ed)?|explicit|hd|hq|kbps"
        r"|free download|out now|visuali[sz]er)\b",
        re.I,
    )


_UTILITY = (
    r"(?<=[(\[])(?:dj |clean |short |extended )?{word}(?: (?:edit|mix|version|tool))?(?=[)\]])"
    r"|{word} (?:edit|mix|version|tool)|(?<=- )(?:dj|clean) {word}$"
)

#: Track.tags derived from version and context text. Order is output order; whole words only. Head words
#: follow trackparse spec/data/version-keywords.json and web-scrobbler metadata-filter (both MIT,
#: see NOTICE). Featured credits ("feat. X", "(with X)") are credits, not versions: no tag.
VERSION_TAGS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (tag, re.compile(rf"(?<!\w)(?:{pattern})(?!\w)", re.I))
    for tag, pattern in (
        ("original-mix", r"original(?: mix)?\s*[)\]]?$|original mix"),
        ("extended-mix", r"extended(?: mix| version)?"),
        ("radio-edit", r"radio (?:edit|mix|version)"),
        # SPDX-SnippetBegin
        # SPDX-License-Identifier: MIT
        # SPDX-SnippetCopyrightText: Copyright (c) 2026 Maksim Maksimov (4matic/trackparse); 2020-2021 Web Scrobbler Team
        # Version head words (remix, edit, bootleg, vip, dub) ported from trackparse and metadata-filter; see NOTICE.
        ("remix", r"remix|rmx|re-?rub"),
        ("edit", r"(?<!radio )edit|re-?edit"),
        ("bootleg", r"bootleg"),
        ("vip", r"vip"),
        ("dub", r"dub (?:mix|version)|(?:remix|mix|into|in) dub|(?<=\()dub(?=\))|(?<=- )dub$"),
        # SPDX-SnippetEnd
        ("instrumental", r"instrumental"),
        ("acapella", r"a ?cappella|acapella"),
        ("live-recording", r"live\s*[)\]]|live (?:@|at|from|session|version|recording)"),
        ("remaster", r"remaster(?:ed)?"),
        ("mashup", r"mash-? ?up"),
        ("flip", r"flip(?=\s*[)\]]|\s*$)"),  # a version word only at the end: "Flip Flop" is a title
        ("rework", r"re-?work"),
        ("refix", r"refix"),
        ("sped-up", r"sped up|speed up|nightcore"),
        ("slowed", r"slowed(?: (?:\+|&|and) reverb)?"),
        # Context kept in the title AND tagged (the maintainer, Fri 2 Oct): "(Sunset Drive VA)", '(From "Voyage")'.
        (
            "soundtrack",
            r"(?<=\()from\s+(?:the\s+)?(?:[\"\u201c'][^\"\u201d']+[\"\u201d']|motion picture|movie|film)"
            r"|(?-i:VA)(?=\s*[)\]])|(?-i:OST)|soundtrack",
        ),
        ("unreleased", r"(?<=[(\[])unreleased(?=[)\]])"),
        # Round 6 version family. "re-edit" also keeps "edit" and "rerub" keeps "remix" (each is one);
        # "Clean Orchard" never tags clean.
        ("re-edit", r"re-?edit"),
        ("rerub", r"re-?rub"),
        ("clean", r"(?<=[(\[])clean(?: version| edit| radio edit)?(?=[)\]])|(?<=- )clean(?: version| edit)?$"),
        ("explicit", r"(?<=[(\[])explicit(?: version| edit)?(?=[)\]])|(?<=- )explicit(?: version)?$"),
        # DJ utility versions: only in version position, so an album track called "Intro" never tags.
        ("intro", _UTILITY.format(word="intro")),
        ("outro", _UTILITY.format(word="outro")),
        ("dj-tool", r"dj[ -]?tools?|(?<=[(\[])tool(?=[)\]])|(?<=- )tool$"),
    )
)

#: "<Name> Mix" names a remixer ("Marlowe Mix", "Jonas Vale Mix") unless every word before "Mix"
#: is a descriptor ("Club Mix", "Techno Mix", "Extended VIP Mix", "7' Mix"): tagged remix.
MIX_DESCRIPTORS: frozenset[str] = frozenset(
    json.loads((Path(__file__).with_name("title_fix_vocab.json")).read_text(encoding="utf-8"))["mix_descriptors"]
)
_MIX_QUALIFIER = re.compile(r"(?:[(\[]|\s-\s)([^()\[\]]*?)\s+mix\s*(?:[)\]]|$)", re.I)


_MULTI_WORD_DESCRIPTORS = tuple(sorted((d for d in MIX_DESCRIPTORS if " " in d), key=len, reverse=True))


def _names_a_remixer(title: str) -> bool:
    for m in _MIX_QUALIFIER.finditer(title):
        text = " ".join(w.strip("'\".,").lower() for w in m.group(1).split())
        for phrase in _MULTI_WORD_DESCRIPTORS:  # a multi-word descriptor is one phrase, not words to test singly
            text = re.sub(rf"\b{re.escape(phrase)}\b", " ", text)
        if any(
            w and not w.isdigit() and not re.fullmatch(r"\d+(?:''|'|\")?", w) and w not in MIX_DESCRIPTORS
            for w in text.split()
        ):
            return True
    return False


def version_tags(title: str) -> tuple[str, ...]:
    return tuple(
        tag for tag, pattern in VERSION_TAGS if pattern.search(title) or (tag == "remix" and _names_a_remixer(title))
    )
