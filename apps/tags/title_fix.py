"""Track title auto-fixer: regex proposes, JEV judges, DeepSeek breaks ties.

Mini-PRD
--------
✔︎ R1 regex_propose strips download junk, MIK key/energy prefixes, key/bpm
   suffixes and track numbers, and lifts an embedded artist out of the title.
   [if] "9A - 7 - Lumenfall" then title "Lumenfall", key 9A, energy 7 [else ⛔️]
   [if] "Video Gardens" by Ida Verrin then unchanged ("video" is not junk) [else ⛔️]
   [if] artist blank, title "Vessel Choir - Ember Tide [Official Music Video]" then
        artist "Vessel Choir", title "Ember Tide" [else ⛔️]
✔︎ R2 judge_with_jev asks JEV (one call) whether the row is music and whether
   the regex proposal is right; a failed call is UNKNOWN, never a pass.
   [if] JEV HTTP error then Verdict.status == "unknown" and nothing applied [else ⛔️]
✔︎ R3 fix_track applies the regex proposal only when JEV agrees (p >= CFG
   threshold); otherwise DeepSeek extracts artist/title and the row is marked
   for review unless DeepSeek independently matches the regex.
   [if] JEV p_ok 0.3 and DeepSeek == regex then source "regex+deepseek" [else ⛔️]
✔︎ R5 version_tags derives kebab-case Track.tags from the cleaned title; the title itself
   keeps its version text ("Original Mix" stays, and also tags original-mix).
   [if] "Neon Harbor (Original Mix)" then tags ("original-mix",) and title unchanged [else ⛔️]
   [if] "Opal - Marrow Remix" then tags ("remix",) [else ⛔️]
   [if] "Remixed Feelings" then no tags (whole-word match only) [else ⛔️]
✔︎ R6 (round 4) leading numbers and junk: a bare "404 - " is a track number only with a filled
   artist or another split after it; a lone "3 - " is MIK energy only with a key or a filled
   artist; cue times, **NEW**, [NCS Release], "- YouTube", Y2Mate tails and full-width
   brackets are junk; "Gloam-404-All The Quiet Things" splits, "Re-Wander" does not; a
   version-only right side ("Slow Harbor - 2003 Remastered") never becomes a split.
   [if] "404 - Cinder", blank artist, then artist "404", title "Cinder" [else ⛔️]
   [if] "3 - Vantor Quill - Glasswork", blank artist, then energy "" and artist "Vantor Quill" [else ⛔️]
   [if] "Slow Harbor - 2003 Remastered", blank artist, then artist "" (review) [else ⛔️]
✔︎ R7 (round 4) a MIK-strip-only change on a filled artist auto-applies at JEV p >= 0.5; a
   DeepSeek artist that appears nowhere in the row is never used (review with the regex artist).
   [if] JEV 0.69 on "6 - Evergreen - ..." by a filled artist then regex+jev, no DeepSeek [else ⛔️]
   [if] DeepSeek says "The 1980" for "1980 - Velvet" then review with artist "1980" [else ⛔️]
✔︎ R8 (round 5) a filled artist field stays verbatim, EXCEPT when it holds the title or a site
   watermark: then a cleaned artist is proposed and the row ALWAYS goes to review.
   [if] artist "Dun Harlow - Say Less (Original Mix) DANCEDJ.CLUB" then artist "Dun Harlow", review [else ⛔️]
   [if] artist "Tallow,Marrow" on "Opal - Marrow Remix" then artist unchanged [else ⛔️]
✔︎ R9 (round 5) soundtrack and unreleased context stays in the title AND is tagged.
   [if] "Pale Hare (Sunset Drive VA)" then title unchanged, tags ("soundtrack",) [else ⛔️]
   [if] "X (Remix) [Unreleased]" then tags ("remix", "unreleased") [else ⛔️]
✔︎ R10 (round 5) explicit covers: performer is the artist, original_artist is filled, a
   "(Guitar Cover)"-style marker stays in the title, tags cover (+ instrument) or karaoke.
   ALWAYS review, never auto-applied.
   [if] 'Iron Fern "Quiet Engine" - guitar cover by fretmoth' then fretmoth / "Quiet Engine
        (Guitar Cover)", original Iron Fern, review [else ⛔️]
   [if] "Salute" by Pine Ogre or "Recovery" then no cover [else ⛔️]
   [if] karaoke then "Originally Performed by" stays in the title [else ⛔️]
✔︎ R11 (round 5) DJ project files: strong code rules AND JEV P(project) >= 0.7 give status
   "project" with tags dj-project + a rule sub-tag; the title is never rewritten. Strong rules
   that JEV disputes go to review. The weak lossless-no-metadata band needs JEV >= 0.8. Owner
   aliases come from $OPENDJ_DJ_ALIASES only (default empty, never committed).
   [if] "Low Ember_capture.wav" then rule "capture" fires [else ⛔️]
   [if] a "_pn.wav" promo with no artist then no rule fires [else ⛔️]
   [if] OPENDJ_DJ_ALIASES is unset then the own-alias rule never fires [else ⛔️]
✔︎ R12 (round 6) provenance tags from the raw row: mik-analyzed, low-bitrate, streaming-only,
   youtube-rip, beatport-sourced, duplicate-copy (tag only; merging belongs to apps/dedup).
   [if] "8A - 6 - X" by a filled artist then mik-analyzed and Proposal.mik_analyzed [else ⛔️]
   [if] a 128 kbps mp3 then low-bitrate; a 128 kbps reading on a .wav or no bitrate then not [else ⛔️]
   [if] filename "Matinee(1).mp3" then duplicate-copy; "Marlowe Dune (Pt. 1).mp3" then not [else ⛔️]
✔︎ R13 (round 6) version family and DJ utility tags: re-edit, rerub, clean, explicit, intro,
   outro, dj-tool, video-source; live-set / dj-mix for blank-artist rows over 20 minutes.
   [if] "Rather Stay - Clean Orchard Remix" then no clean tag [else ⛔️]
   [if] album track "Intro" then no intro tag; "Song (Intro Edit)" then intro [else ⛔️]
   [if] a 22-minute album track with a filled artist then no dj-mix tag [else ⛔️]
✔︎ R14 (round 6) featured artists from feat./ft./featuring/(with X) in the title AND artist field
   go to Proposal.featured_artists; the title text is kept as-is.
   [if] "Love Song (feat. Glasses)" then featured ("Glasses",) and title unchanged [else ⛔️]
   [if] "Featuring Love" by A then no featured artists [else ⛔️]
✔︎ R15 (round 6) "- prod. X" / "(prod. X)" moves to Proposal.producer; a trailing "- A Vs B" tail
   that only repeats the artist field is stripped.
   [if] "Freed From Doubt - prod. Marrow Vell, Pip Jay" then title "Freed From Doubt" [else ⛔️]
   [if] "Dust - Remy Calder Vs. Cotonwell Remix" then unchanged [else ⛔️]
✔︎ R16 (round 6) ALL CAPS goes to review only with 3+ words AND junk/credit words; mojibake is
   re-decoded only when the round trip is clean; a file extension in the title is stripped; a
   known Windows-illegal substitution is undone and an unknown one goes to review; an exactly
   30-char title extended by the filename is proposed for review.
   [if] "RIOT", "MODEST.", "S.W.A.Y.", "I WON'T GO" then no review [else ⛔️]
   [if] "Der B\u00c3\u00a4r Groovt" then "Der B\u00e4r Groovt"; "\u00c3me" then unchanged [else ⛔️]
   [if] artist "u_v" then unchanged [else ⛔️]
✔︎ R17 (round 6) mashups: " x " / " vs " candidates get a JEV mashup question; collaborations
   ("Kestrel Fold x Hollow Parade") must not tag. Candidates never take the safe tier.
   [if] "Tangerine X Crowns Will Roll" then JEV is asked and p >= CFG then tag mashup [else ⛔️]
✔︎ R18 (round 6) safe tier: when every change is in SAFE_TRANSFORMS (each audited on the full
   library and against AcoustID/MusicBrainz), the row auto-applies with source "safe-tier" and
   NO JEV or DeepSeek call. Sampler rows are project with no JEV.
   [if] "8A - 6 - Ember Tide" by Vessel Choir then fixed, source safe-tier, cost 0 [else ⛔️]
   [if] "6 - Ember Tide [Official Video]" by Vessel Choir then JEV is called [else ⛔️]
✔︎ R19 (round 7) every OpenRouter call has a HARD wall-clock deadline (CFG.CALL_WALL_S, not only httpx's
   per-read timeout) and fix_tracks takes an optional whole-run wall budget; a call past either deadline
   makes the row UNKNOWN with the reason in Verdict.error. The key is an argument (api_key); the
   environment is only the default for the CLI and the eval.
   [if] a call outlives CFG.CALL_WALL_S then the row is unknown, error names the wall deadline [else ⛔️]
   [if] the run budget is spent then later rows make no call and are unknown [else ⛔️]
   [if] fix_tracks(rows, api_key="k") with no environment key then the calls carry "k" [else ⛔️]
✔︎ R20 (round 7) youtube-rip needs a converter marker (YT2mp3, y2mate, onlymp3, SnapSave, a YouTube id
   tail, "- YouTube"); a bare "(320kbps)" is not one. Sampler rows never take dj-project-gig-intro-or-sting.
   Lyric and translation notes ("(Lyrics)", "(Lyric Video)", "(English lyrics translation)") stay in the
   title and tag video-source.
   [if] filename "Song (320kbps).mp3" then no youtube-rip [else ⛔️]
   [if] "Maple Hollow (English lyrics translation)" then title kept, tag video-source [else ⛔️]
✔︎ R21 (round 7) a regex+deepseek agreement that drops bracketed text outside the strip list
   (Rx.STRIPPABLE_BRACKET) goes to review instead of auto-applying.
   [if] raw "Song (Rare Mix)" and both say "Song" then review [else ⛔️]
   [if] raw "Song (Official Video)" and both say "Song" then fixed [else ⛔️]
→  R4 writeback to rekordbox/file tags (goes through apps.tags.apply later).
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, TypeVar

import httpx


class CFG:
    OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"
    JEV_URL = "https://openrouter.ai/api/alpha/decisions"
    JEV_MODEL = "~typesafe/jev-latest"
    CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
    FALLBACK_MODEL = "deepseek/deepseek-v4.1-flash"
    AGREE_THRESHOLD = 0.8  # JEV P(proposal correct) needed to auto-apply regex
    MIK_ONLY_THRESHOLD = 0.5  # lower bar when the only change is a MIK prefix strip on a filled artist
    NOT_MUSIC_THRESHOLD = 0.6
    PROJECT_THRESHOLD = 0.7  # JEV P(project) needed on top of a strong project rule
    PROJECT_MAYBE_THRESHOLD = 0.8  # JEV P(project) needed when only the weak band fired
    STING_MAX_S = 20  # a lossless clip with no metadata shorter than this is a gig sting
    LONG_RECORDING_S = 1200  # over 20 minutes with no artist: a DJ mix or live set
    LOW_BITRATE_KBPS = 192  # a lossy file at or under this bitrate tags low-bitrate
    KIND_SUM_TOLERANCE = 0.05  # music + not_music must sum to 1 within this, else the JEV answer is incoherent
    MASHUP_THRESHOLD = 0.5  # JEV P(mashup) to tag an " x " / " vs " candidate (round 6: collabs <= 0.10)
    MASHUP_REVIEW = True  # JEV-tagged mashups go to review, never auto-apply (the shipped contract; False only with a measured precision >= 95%)
    DJ_ALIASES_ENV = "OPENDJ_DJ_ALIASES"  # comma-separated owner artist names; per-user config only
    STRIP_MIK_PREFIX = True  # parsed key/energy are kept on the Proposal
    TIMEOUT_S = 20.0  # httpx connect / per-read timeout; NOT a bound on a whole call (see CALL_WALL_S)
    # Hard wall deadline for ONE OpenRouter call. Full run Fri 2 Oct: DeepSeek rows p50 3 s, p99 43 s, max 61 s;
    # 2/10,138 rows stalled past 150 s because OpenRouter keeps a slow call alive, so the read timeout never fired.
    CALL_WALL_S = 90.0
    PROBE_CALLS = 3  # calls of one model that must settle, one at a time, before that model's calls fan out
    CONCURRENT_RESERVE_MULT = 2.0  # a call dispatched beside others reserves this many times the largest cost seen
    CALL_RESERVE_USD = 0.002  # held against the spend cap per in-flight call; what an abandoned call is charged
    WORKERS = 8


Status = Literal["clean", "fixed", "review", "not_music", "project", "unknown"]


@dataclass(frozen=True)
class Track:
    id: str
    title: str
    artist: str
    filename: str = ""
    # Optional library context for DJ project detection. None means unknown, never "absent".
    folder: str = ""
    album: str | None = None
    label: str | None = None
    year: int | None = None
    length_s: int | None = None
    bit_depth: int | None = None
    sample_rate: int | None = None
    bitrate: int | None = None  # kbps; None (or 0 from a library) is unknown


@dataclass(frozen=True)
class Proposal:
    artist: str
    title: str
    key: str = ""
    energy: str = ""
    bpm: str = ""
    tags: tuple[str, ...] = ()
    original_artist: str = ""
    review_reasons: tuple[str, ...] = ()  # set => never auto-applied
    mik_analyzed: bool = False  # the raw title carried a Mixed In Key prefix (key precedence reads this)
    featured_artists: tuple[str, ...] = ()
    producer: str = ""
    transforms: tuple[str, ...] = ()  # named edits regex_propose made, in order (see SAFE_TRANSFORMS)


@dataclass(frozen=True)
class Verdict:
    status: Status
    artist: str
    title: str
    source: str
    regex: Proposal
    jev_kind: str = ""
    jev_p_ok: float | None = None
    llm: Proposal | None = None
    error: str = ""
    cost_usd: float = 0.0
    notes: list[str] = field(default_factory=list)
    tags: tuple[str, ...] = ()
    original_artist: str = ""
    featured_artists: tuple[str, ...] = ()
    producer: str = ""
    jev_p_mashup: float | None = None


# ----------------------------------------------------------------- regex


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


# ----------------------------------------------------------------- openrouter


class CallTimeout(RuntimeError):
    """An OpenRouter call passed its wall deadline: CFG.CALL_WALL_S, or the run's wall budget."""

    charged_usd: float = 0.0  # set when the budget charged the abandoned call its reserve


SPEND_CAP_ERROR = "spend cap reached"


T = TypeVar("T")


class PaidCallError(RuntimeError):
    """A call was paid for and its answer then could not be used (missing fields, wrong shape). charged_usd is what
    the budget took for it, so the row's stored verdict accounts for the call instead of reading as free."""

    def __init__(self, message: str, charged_usd: float) -> None:
        super().__init__(message)
        self.charged_usd = charged_usd


def charged_usd(exc: BaseException) -> float:
    """What the spend budget charged for the call that raised ``exc`` (0 when it was never billed)."""
    return float(getattr(exc, "charged_usd", 0.0))


def _parse_billed(data: Any, parse: Callable[[], T]) -> T:
    """Run an answer parser; any failure becomes a PaidCallError carrying the cost the budget took for ``data``."""
    try:
        return parse()
    except Exception as exc:
        raise PaidCallError(str(exc), _reported_cost(data) or 0.0) from exc


class UnknownCostError(RuntimeError):
    """A call got an HTTP 200 and then failed (unreadable body, broken stream): it may have been billed, so its
    reserve is charged rather than released."""

    charged_usd: float = 0.0


class SpendBudget:
    """A run's OpenRouter spend, shared by every worker thread.

    A call RESERVES against the cap before it is dispatched, so calls already in flight count and a scan cannot
    overshoot by the cost of every concurrent call. The estimate is only a guess until a real cost has been seen,
    and a cost is only comparable within one COST CLASS (the model the call is billed on: a cheap JEV call says
    nothing about a DeepSeek call). So the first CFG.PROBE_CALLS calls of each class run one at a time, and only
    once that many have settled may the class fan out. From then on a reservation is the largest cost observed in
    its class (CFG.CONCURRENT_RESERVE_MULT times that when other calls are in flight), never less than the
    estimate, and a call that does not fit beside the calls in flight waits for them. The reserve is settled to
    the real cost on a response, released when the call failed before it could be billed, or kept as the
    charge when the call was abandoned at its wall deadline (its real cost is unknown, so it is never
    free). Without a per-request token cap the cost of a call has no hard upper bound, so a call dearer
    than the multiple of everything seen before it can still pass the cap by its excess."""

    def __init__(self, limit_usd: float) -> None:
        self.limit_usd = limit_usd
        self.spent_usd = 0.0
        self.reserved_usd = 0.0
        self._max_observed_usd: dict[str, float] = {}  # cost class -> largest settled cost
        self._settled: dict[str, int] = {}  # cost class -> calls that settled with a reported cost
        self._in_flight: dict[str, int] = {}
        self._lock = threading.Condition()

    def reserve(self, estimate_usd: float, cost_class: str = "", deadline: float | None = None) -> float:
        """Reserve for one call and return the amount reserved, which the caller must hand back to settle, release
        or abandon (with the same cost_class). Waits while its class's probe call is in flight, and while other
        calls are in flight and the headroom cannot cover this one on top of them (calls near the cap run one at a
        time, so a call dearer than its predecessors overshoots by its own excess only). Raises SpendCapReached
        when nothing is in flight and the cap still leaves no room. Without a ``deadline`` the wait has no timeout:
        every call in flight ends within the call wall (settled, released or abandoned) and each of those wakes the
        waiters, so admission always comes; a fixed timeout would drop rows while a provider recovers behind abandoned
        probes, which never advance the probe count. With a ``deadline`` (a time.monotonic() stamp, the run's
        whole-run wall budget) it never waits past it."""
        with self._lock:
            wait_s = None if deadline is None else max(0.0, deadline - time.monotonic())
            if not self._lock.wait_for(lambda: self._admissible(estimate_usd, cost_class), timeout=wait_s):
                raise CallTimeout("the spend budget did not free up within the run's wall budget")
            amount = self._amount(estimate_usd, cost_class)
            if not self._fits(amount):
                raise SpendCapReached(
                    f"{SPEND_CAP_ERROR}: ${self.spent_usd:.4f} spent and ${self.reserved_usd:.4f} in flight "
                    f"of ${self.limit_usd:.4f}"
                )
            self.reserved_usd += amount
            self._in_flight[cost_class] = self._in_flight.get(cost_class, 0) + 1
            return amount

    def _amount(self, estimate_usd: float, cost_class: str) -> float:
        observed = self._max_observed_usd.get(cost_class, 0.0)
        beside_others = self._any_in_flight()
        return max(estimate_usd, observed * (CFG.CONCURRENT_RESERVE_MULT if beside_others else 1.0))

    def _fits(self, amount_usd: float) -> bool:
        return self.spent_usd + self.reserved_usd + amount_usd <= self.limit_usd

    def _admissible(self, estimate_usd: float, cost_class: str) -> bool:
        probed = self._settled.get(cost_class, 0) >= CFG.PROBE_CALLS or self._in_flight.get(cost_class, 0) == 0
        return probed and (self._fits(self._amount(estimate_usd, cost_class)) or not self._any_in_flight())

    def settle(self, reserved_usd: float, actual_usd: float, cost_class: str = "") -> None:
        with self._lock:
            self._return_reserve(reserved_usd, cost_class)
            self.spent_usd += actual_usd
            self._max_observed_usd[cost_class] = max(self._max_observed_usd.get(cost_class, 0.0), actual_usd)
            self._settled[cost_class] = self._settled.get(cost_class, 0) + 1
            self._lock.notify_all()

    def release(self, reserved_usd: float, cost_class: str = "") -> None:
        with self._lock:
            self._return_reserve(reserved_usd, cost_class)
            self._lock.notify_all()

    def abandon(self, reserved_usd: float, cost_class: str = "") -> None:
        """Charge the reserve and learn nothing: an abandoned call never showed its real cost, so it does not end
        the probe phase and the next call of its class is dispatched alone."""
        with self._lock:
            self._return_reserve(reserved_usd, cost_class)
            self.spent_usd += reserved_usd
            self._lock.notify_all()

    def _any_in_flight(self) -> bool:
        return sum(self._in_flight.values()) > 0

    def _return_reserve(self, reserved_usd: float, cost_class: str) -> None:
        self.reserved_usd -= reserved_usd
        self._in_flight[cost_class] -= 1
        if not self._any_in_flight():
            self.reserved_usd = 0.0  # nothing is held: shed the float residue of unequal reservations coming back


class SpendCapReached(RuntimeError):
    """The run's spend budget was spent before this call started: the row is left unknown, not guessed."""


@dataclass(frozen=True)
class Api:
    """How a row reaches OpenRouter. key None reads $OPENROUTER_API_KEY at call time (the CLI and eval
    default); deadline is a time.monotonic() stamp after which no call starts (the run's wall budget)."""

    key: str | None = None
    deadline: float | None = None
    budget: SpendBudget | None = None

    def __post_init__(self) -> None:
        if self.key is not None and not self.key.strip():
            raise ValueError("api_key was passed but is blank; pass None to read the environment")


def _api_key(api: Api) -> str:
    key = (api.key if api.key is not None else os.environ.get(CFG.OPENROUTER_KEY_ENV, "")).strip()
    if not key:
        raise RuntimeError(
            f"{CFG.OPENROUTER_KEY_ENV} is not set and no api_key was passed; title fixing needs OpenRouter"
        )
    return key


def _call_wall_s(api: Api) -> float:
    if api.deadline is None:
        return CFG.CALL_WALL_S
    left = api.deadline - time.monotonic()
    if left <= 0:
        raise CallTimeout("run wall budget spent before this call started")
    return min(CFG.CALL_WALL_S, left)


def _read_response(url: str, body: dict[str, Any], key: str, stop: threading.Event) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {key}", "X-Title": "Open DJ"}
    with httpx.stream("POST", url, json=body, headers=headers, timeout=CFG.TIMEOUT_S) as response:
        chunks: list[bytes] = []
        try:
            for chunk in response.iter_bytes():
                if stop.is_set():  # abandoned at the wall deadline: leaving the block closes the stream
                    break
                chunks.append(chunk)
        except Exception as exc:
            if response.status_code == 200:
                raise UnknownCostError(f"{url} answered 200 and the stream then failed: {exc}") from exc
            raise
    if stop.is_set():
        raise CallTimeout(f"{url} abandoned at its wall deadline")
    raw = b"".join(chunks)
    if response.status_code != 200:
        raise RuntimeError(f"{url} returned HTTP {response.status_code}: {raw[:200].decode(errors='replace')}")
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise UnknownCostError(f"{url} answered 200 with an unreadable body: {exc}") from exc


def _post(url: str, body: dict[str, Any], api: Api) -> dict[str, Any]:
    """One OpenRouter call under a HARD wall deadline. httpx's timeout bounds each socket read, and OpenRouter
    keeps a slow call alive with whitespace, so a stalled call never trips it: the call runs on a daemon thread
    that is abandoned at the deadline and closes its own stream on the next chunk."""
    key, cost_class = _api_key(api), str(body["model"])
    reserve = 0.0 if api.budget is None else api.budget.reserve(CFG.CALL_RESERVE_USD, cost_class, api.deadline)
    try:
        wall = _call_wall_s(api)  # after the reservation wait: the run's deadline may have passed behind a probe
    except CallTimeout:
        if api.budget is not None:
            api.budget.release(reserve, cost_class)
        raise
    outcome: dict[str, Any] = {}
    stop = threading.Event()

    def call() -> None:
        try:
            outcome["data"] = _read_response(url, body, key, stop)
        except Exception as exc:  # noqa: BLE001 - not swallowed: re-raised on the caller thread below
            outcome["error"] = exc

    worker = threading.Thread(target=call, name="openrouter-call", daemon=True)
    worker.start()
    worker.join(wall)
    if worker.is_alive():
        stop.set()
        if api.budget is not None:
            api.budget.abandon(reserve, cost_class)  # dispatched and possibly billed: charged its reserve, never free
        timeout = CallTimeout(f"{url} passed its {wall:.0f} s wall deadline")
        timeout.charged_usd = reserve
        raise timeout
    if "error" in outcome:
        if api.budget is not None:
            if isinstance(outcome["error"], UnknownCostError):
                api.budget.abandon(reserve, cost_class)  # a 200 was returned: possibly billed, so never free
                outcome["error"].charged_usd = reserve
            else:
                api.budget.release(reserve, cost_class)  # failed before a response: nothing was billed
        raise outcome["error"]
    if api.budget is not None:
        _settle_response(api.budget, reserve, cost_class, url, outcome["data"])
    return outcome["data"]


def _settle_response(budget: SpendBudget, reserve: float, cost_class: str, url: str, data: Any) -> None:
    """Settle a response to the cost it reports. One that reports none is charged its reserve (a billable call is never
    free) and then FAILS the row with a PaidCallError carrying that charge: the response is never rewritten into a
    success with an invented cost."""
    cost = _reported_cost(data)
    if cost is not None:
        budget.settle(reserve, cost, cost_class)
        return
    budget.abandon(reserve, cost_class)
    shape = (
        "a JSON object without a valid usage.cost"
        if isinstance(data, dict)
        else f"a {type(data).__name__}, not a JSON object"
    )
    raise PaidCallError(f"{url} answered 200 with {shape}", reserve)


def _reported_cost(data: Any) -> float | None:
    """usage.cost as a finite, non-negative number, else None (unknown: the caller charges its reserve)."""
    usage = data.get("usage") if isinstance(data, dict) else None
    cost = usage.get("cost") if isinstance(usage, dict) else None
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
        return None
    return float(cost)


def _required_cost(data: Any) -> float:
    """usage.cost when it is a finite non-negative number; anything else fails the row instead of reading as free."""
    cost = _reported_cost(data)
    if cost is None:
        raise ValueError("the response reports no valid usage.cost")
    return cost


def _describe(track: Track, proposal: Proposal) -> str:
    return (
        f"RAW title: {track.title}\nRAW artist: {track.artist or '(blank)'}\n"
        f"filename: {track.filename}\n\n"
        f"PROPOSED artist: {proposal.artist or '(blank)'}\nPROPOSED title: {proposal.title}"
        + (
            f"\nPROPOSED producer (credit removed from the title, recorded on the proposal only): {proposal.producer}"
            if proposal.producer
            else ""
        )
    )


JEV_QUESTIONS: dict[str, Any] = {
    "kind": {
        "type": "choice",
        "instructions": "What is this library row?",
        "criteria": {
            "music": "a song or DJ track by an artist, including remixes, edits, live sets",
            "not_music": "a one-shot sample (CLAP, HORN), a recording capture, a streaming URI, "
            "or a DJ's own unreleased project file with no real song identity",
        },
    },
    "proposal_ok": {
        "type": "noul",
        "instructions": "Are the PROPOSED artist and PROPOSED title both exactly right for this "
        "song? Title must keep remix/edit/version names, featured artists and lyric or translation "
        "notes like '(Lyrics)' or '(English lyrics translation)', but drop file "
        "junk (bitrates, [Official Video], site names, track numbers, key/energy tags). Artist "
        "must be the performing artist, never blank when the raw text names one. Answer no if "
        "any word of the real title was lost or the artist is wrong or missing.",
    },
}


MASHUP_QUESTION: dict[str, Any] = {
    "type": "noul",
    "instructions": "Is this row a MASHUP: one track that blends two or more DIFFERENT existing songs, "
    "e.g. 'Ember X Pale Hours' or 'Song A vs Song B (DJ Mashup)'? Answer NO when ' x ', 'X' or "
    "'vs' joins ARTIST names in a collaboration or a remix credit, e.g. 'Kestrel Fold x Hollow "
    "Parade - Lights Out Again', 'Last Night - Vantor Quill x Lio Marrow Remix', 'Waste (Q Z Remix)', "
    "or 'We Are Neighbors - Tallow Vs. Marrow'.",
}


@dataclass(frozen=True)
class JevAnswer:
    kind: str
    p_ok: float
    p_mashup: float | None
    cost: float


def mashup_candidate(track: Track, proposal: Proposal) -> bool:
    """ " x " / " vs " in the title with no explicit mashup word: only JEV can tell a mashup from a collab."""
    return "mashup" not in proposal.tags and bool(Rx.MASHUP_JOIN.search(f"{track.title} | {proposal.title}"))


def judge_with_jev(track: Track, proposal: Proposal, api: Api) -> JevAnswer:
    """One JEV call: kind, P(proposal ok) and, for " x " candidates only, P(mashup). Raises on any failure."""
    ask_mashup = mashup_candidate(track, proposal)
    questions = {**JEV_QUESTIONS, "mashup": MASHUP_QUESTION} if ask_mashup else JEV_QUESTIONS
    data = _post(
        CFG.JEV_URL, {"model": CFG.JEV_MODEL, "state": _describe(track, proposal), "questions": questions}, api
    )
    return _parse_billed(data, lambda: _jev_answer(data, ask_mashup))


def _probability(value: object, what: str) -> float:
    """A model-reported probability: a number in [0, 1] (NaN and infinity fall outside it), never a bool (float(True) would read as certain)."""
    if (
        isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0
    ):  # the range test is also False for NaN
        raise ValueError(f"{what} is not a probability in [0, 1]: {value!r}")
    return float(value)


def _jev_answer(data: dict[str, Any], ask_mashup: bool) -> JevAnswer:
    answers = data["answers"]
    kind_probs = answers["kind"]["probabilities"]
    p_not_music = _probability(kind_probs["not_music"], "kind.not_music")  # required: a missing one is no answer
    p_music = _probability(kind_probs["music"], "kind.music")
    if abs(p_music + p_not_music - 1.0) > CFG.KIND_SUM_TOLERANCE:
        raise ValueError(f"kind probabilities are not a distribution: music {p_music} + not_music {p_not_music}")
    kind = "not_music" if p_not_music >= CFG.NOT_MUSIC_THRESHOLD else "music"
    p_mashup = _probability(answers["mashup"]["noul"], "mashup.noul") if ask_mashup else None
    p_ok = _probability(answers["proposal_ok"]["noul"], "proposal_ok.noul")
    return JevAnswer(kind, p_ok, p_mashup, _required_cost(data))


LLM_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "track_identity",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"artist": {"type": "string"}, "title": {"type": "string"}},
            "required": ["artist", "title"],
            "additionalProperties": False,
        },
    },
}

LLM_SYSTEM = (
    "You clean DJ library metadata. Return the performing artist(s) and the song title. "
    "Keep remix/edit/version names and featured artists in the title, e.g. "
    "'Opal - Marrow Remix' or 'Love Song (feat. Glasses)'. Keep lyric or translation notes in the title too, "
    "e.g. '(Lyrics)', '(Lyric Video)' or '(English lyrics translation)'. Drop file junk: bitrates, "
    "[Official Video], site names, track numbers, Mixed In Key energy/key tags like '6 - ' "
    "or '9A - 7 - ', trailing key/bpm like ' - Fm - 125', '_capture', '(1)', and producer credits like "
    "' - prod. X' or '(prod. by X)'. "
    "If no artist is named anywhere, return an empty artist string: never invent or "
    "guess one and never write 'Unknown'. "
    "When RAW artist is filled, never drop leading words of the raw title unless they repeat "
    "that artist: 'Evergreen - Quiet People (Club Mix) [Mixed]' keeps 'Evergreen - ' and '[Mixed]'. "
    "In a DJ project or bounce file, a leading NAME_ prefix names the artist, e.g. "
    "'VESPR_Lowtide Breaks AA 1644 MASTER' is artist 'VESPR', title 'Lowtide Breaks'."
)


def extract_with_llm(track: Track, api: Api) -> tuple[Proposal, float]:
    data = _post(
        CFG.CHAT_URL,
        {
            "model": CFG.FALLBACK_MODEL,
            "messages": [
                {"role": "system", "content": LLM_SYSTEM},
                {"role": "user", "content": _describe(track, Proposal("", "")).split("\n\n")[0]},
            ],
            "response_format": LLM_SCHEMA,
            "temperature": 0,
            "usage": {"include": True},
        },
        api,
    )
    return _parse_billed(data, lambda: _llm_proposal(track, data))


def _llm_proposal(track: Track, data: dict[str, Any]) -> tuple[Proposal, float]:
    content = data["choices"][0]["message"].get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(
            f"{CFG.FALLBACK_MODEL} returned no content (finish={data['choices'][0].get('finish_reason')})"
        )
    parsed = json.loads(content)
    cost = _required_cost(data)
    # A filled artist field is the user's data: the LLM only ever fixes the title then.
    artist = track.artist.strip() or parsed["artist"].strip()
    title = parsed["title"].strip()
    return Proposal(artist=artist, title=title, tags=version_tags(title)), cost


# ----------------------------------------------------------------- DJ project files
#: Ported from the round-5 prototype (strong rules AND JEV), measured on the private projects fixture set (scripts/title_fix_eval.py).


class Proj:
    SAMPLER_FOLDER = re.compile(r"/(?:PioneerDJ|rekordbox)/Sampler/", re.I)
    RECORDING_FOLDER = re.compile(r"/(?:PioneerDJ|rekordbox)/Recording/", re.I)
    SAMPLER_FILE = re.compile(r"MergeFX Sample Sound", re.I)
    CAPTURE_FILE = re.compile(r"_capture(?:\(\d+\))?\.(?:wav|aiff?)$", re.I)
    REC_FILE = re.compile(r"(?:^|\s)REC-\d{4}-\d{2}-\d{2}")
    MASTERING_SERVICE = re.compile(
        r"^LANDR-|(?<![A-Za-z])e-?mastered|-(?:Balanced|Warm|Open)-(?:Low|Medium|High)\b", re.I
    )
    DAW_EXPORT_CS = re.compile(r"(?<![A-Za-z])MASTER(?:ED)?(?![A-Za-z])")  # case-sensitive: "AA 1644 MASTER"
    DAW_EXPORT_CI = re.compile(r"_mastered|_mixdown\d*|_bounce\d*|(?<![a-z])(?:16|24|32)[ -]?bit(?![a-z])", re.I)
    GIG_WORDS = re.compile(r"\b(?:intro|outro|dj tool|sting|jingle|birthday|drop)\b", re.I)
    ALIAS_PREFIX = re.compile(r"^([A-Z][A-Z0-9]{2,})_")
    PROMO_OR_STORE = re.compile(r"_pn\.(?:wav|aiff?)$|^\d{6,}_", re.I)
    LOSSLESS = (".wav", ".aif", ".aiff")


def dj_aliases() -> tuple[str, ...]:
    """The library owner's own artist names, from per-user config only. Empty when unset."""
    return tuple(a.strip() for a in os.environ.get(CFG.DJ_ALIASES_ENV, "").split(",") if a.strip())


def _known_absent(value: str | int | None) -> bool:
    return value is not None and not value


def _is_daw_export(track: Track, name: str, *, released: bool, promo: bool) -> bool:
    if released or promo:
        return False
    return bool(Proj.DAW_EXPORT_CS.search(name) or Proj.DAW_EXPORT_CI.search(track.filename))


def _is_gig_intro_or_sting(track: Track, name: str, *, sampler: bool, bare: bool, promo: bool) -> bool:
    """A lossless, artist-less, metadata-less file named like a gig intro, or a clip too short to be a song.
    A sampler one-shot is a sample-pack sound, not a gig intro (round 7, both judges; 25 library rows)."""
    lossless = track.filename.lower().endswith(Proj.LOSSLESS)
    if sampler or not bare or promo or not lossless or track.artist.strip():
        return False
    short_clip = track.length_s is not None and 0 < track.length_s < CFG.STING_MAX_S
    return bool(Proj.GIG_WORDS.search(name)) or short_clip


def _is_own_alias(track: Track, *, released: bool) -> bool:
    aliases = {a.casefold() for a in dj_aliases()}
    if released or not aliases:
        return False
    prefix = Proj.ALIAS_PREFIX.match(track.filename)
    return track.artist.strip().casefold() in aliases or bool(prefix and prefix.group(1).casefold() in aliases)


def _weak_project_rules(track: Track, strong: tuple[str, ...], *, bare: bool, promo: bool) -> tuple[str, ...]:
    lossless = track.filename.lower().endswith(Proj.LOSSLESS)
    no_artist = not track.artist.strip()
    return ("lossless-no-metadata",) if not strong and bare and lossless and no_artist and not promo else ()


def project_rules(track: Track) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Returns (strong, weak) rule names. Unknown metadata (None) never counts as absent."""
    path, name = f"{track.folder}/{track.filename}", f"{track.title} | {track.filename}"
    released = bool(track.album) or bool(track.label)
    bare = _known_absent(track.album) and _known_absent(track.label) and _known_absent(track.year)
    promo = bool(Proj.PROMO_OR_STORE.search(track.filename))
    sampler = bool(Proj.SAMPLER_FOLDER.search(path) or Proj.SAMPLER_FILE.search(track.filename))
    checks = (
        ("sampler", sampler),
        ("capture", bool(Proj.CAPTURE_FILE.search(track.filename))),
        ("recording", bool(Proj.RECORDING_FOLDER.search(path) or Proj.REC_FILE.search(track.filename))),
        ("mastering-service", bool(Proj.MASTERING_SERVICE.search(track.filename))),
        ("daw-export", _is_daw_export(track, name, released=released, promo=promo)),
        ("gig-intro-or-sting", _is_gig_intro_or_sting(track, name, sampler=sampler, bare=bare, promo=promo)),
        ("own-alias", _is_own_alias(track, released=released)),
    )
    strong = tuple(rule for rule, fired in checks if fired)
    return strong, _weak_project_rules(track, strong, bare=bare, promo=promo)


def project_decision(strong: tuple[str, ...], weak: tuple[str, ...], p_project: float) -> str:
    """ "project", "review" (strong rules that JEV disputes) or "" (not a project file)."""
    if strong and p_project >= CFG.PROJECT_THRESHOLD:
        return "project"
    if strong:
        return "review"
    if weak and p_project >= CFG.PROJECT_MAYBE_THRESHOLD:
        return "project"
    return ""


def project_tags(rules: tuple[str, ...]) -> tuple[str, ...]:
    return ("dj-project", *(f"dj-project-{rule}" for rule in rules))


PROJECT_INSTRUCTIONS = (
    "Is this library row a DJ's OWN PROJECT FILE rather than a released song? Answer YES for: "
    "(1) anything in a Sampler, Capture or Recording folder, including one-shots (CLAP, HORN, LIFTER) "
    "even when a sample-pack name like Loopmasters fills the artist field, and '_capture' grabs of released "
    "songs; (2) the DJ's own masters, bounces, mixdowns and renders ('MASTER', '_mastered', '_mixdown', "
    "'LANDR-', 'eMastered', '24bit', version tags like 'v5-Balanced-Medium'); (3) short intros, outros, "
    "stings, DJ tools or edits the DJ made for a specific gig, venue, party or person, usually a lossless "
    "file with no artist, album or label (a clip under 20 s with no metadata is a sting); (4) any file "
    "whose artist or filename prefix is one of the library owner's own DJ names. Answer NO for songs "
    "released by other artists, INCLUDING remixes, edits, bootlegs, mashups, white labels, unreleased IDs, "
    "promo files ('_pn', Beatport numeric ids), YouTube rips, downloaded DJ sets by named artists, and "
    "album tracks titled 'Intro', 'Outro', 'Untitled', 'Remastered' or with 'Project' or 'Master' inside a "
    "band or remix name. Missing metadata alone is not enough for YES."
)


def _describe_project(track: Track) -> str:
    def known(value: str | int | None) -> str:
        return "unknown" if value is None else ("present" if value else "ABSENT")

    def num(value: int | None, unit: str) -> str:
        return "unknown" if value is None else f"{value}{unit}"

    ext = Path(track.filename).suffix.lower() or "(none)"
    return (
        f"title: {track.title}\nartist: {track.artist or '(blank)'}\nfilename: {track.filename}\n"
        f"folder: {track.folder or 'unknown'}\nformat: {ext}, {num(track.bit_depth, '-bit')}, "
        f"{num(track.sample_rate, ' Hz')}, {num(track.length_s, ' s')}\n"
        f"library owner's own DJ names: {', '.join(dj_aliases()) or '(none configured)'}\n"
        f"metadata: album {known(track.album)}, label {known(track.label)}, release year {known(track.year)}"
    )


def judge_project_with_jev(track: Track, api: Api) -> tuple[float, float]:
    """Returns (p_project, cost). Raises on any failed call."""
    data = _post(
        CFG.JEV_URL,
        {
            "model": CFG.JEV_MODEL,
            "state": _describe_project(track),
            "questions": {"project": {"type": "noul", "instructions": PROJECT_INSTRUCTIONS}},
        },
        api,
    )
    return _parse_billed(
        data, lambda: (_probability(data["answers"]["project"]["noul"], "project.noul"), _required_cost(data))
    )


# ----------------------------------------------------------------- pipeline


def _same(a: Proposal, b: Proposal) -> bool:
    """Punctuation-blind, case-SENSITIVE: 'CANT STOP IT' is not 'Can't Stop It'."""
    norm = lambda s: re.sub(r"\W+", "", s)  # noqa: E731
    return norm(a.artist) == norm(b.artist) and norm(a.title) == norm(b.title)


def _shouted(title: str, raw_title: str) -> bool:
    """ALL CAPS is artist styling (RIOT, MODEST., S.W.A.Y., I WON'T GO) unless the raw title is 3+ words
    AND carries junk/credit words (official, video, feat., deluxe...). A lookup tier may add disagreement later."""
    if sum(c.isupper() for c in title) <= 3 or any(c.islower() for c in title):
        return False
    raw = raw_title or title
    return len(raw.split()) >= 3 and bool(Rx.CAPS_JUNK.search(raw))


def _needs_human(proposal: Proposal, raw_title: str = "") -> str:
    """Script checks JEV is bad at: things that are MISSING or need counting."""
    if Rx.PLACEHOLDER_ARTIST.fullmatch(proposal.artist):
        return "no artist found anywhere"
    if not proposal.title.strip():
        return "empty title"
    if "\ufffd" in proposal.title:
        return "title has a replacement character (lost bytes)"
    if _shouted(proposal.title, raw_title):
        return "title is ALL CAPS with junk or credit words"
    return ""


def _mik_strip_only(track: Track, proposal: Proposal) -> bool:
    """The regex only removed a MIK key/energy prefix and the artist field was already filled."""
    if not (track.artist.strip() and proposal.energy and proposal.artist == track.artist.strip()):
        return False
    m = Rx.MIK_PREFIX.match(track.title.strip())
    return bool(m) and track.title.strip()[m.end() :].strip() == proposal.title


def _fold(text: str) -> str:
    return re.sub(r"\W+", "", text).casefold()


def _artist_in_row(artist: str, track: Track) -> bool:
    """Never invent artists: every credited name must appear somewhere in the raw row."""
    row = _fold(" ".join((track.title, track.artist, track.filename)))
    names = re.split(r"\s*(?:,|&|\+|\bx\b|\band\b|\bfeat\.?|\bft\.?)\s*", artist, flags=re.I)
    return all(_fold(name) in row for name in names if _fold(name))


def _artist_is_title_head(artist: str, proposal: Proposal) -> bool:
    """The regex found no artist separator, yet the LLM artist is the title's leading words: it took
    the song name for the artist (e.g. from a "Tumbleweed Road - ..." filename)."""
    return not proposal.artist and bool(_fold(artist)) and _fold(proposal.title).startswith(_fold(artist))


def _agree_threshold(track: Track, proposal: Proposal) -> float:
    return CFG.MIK_ONLY_THRESHOLD if _mik_strip_only(track, proposal) else CFG.AGREE_THRESHOLD


API_ERRORS = (httpx.HTTPError, RuntimeError, KeyError, ValueError, TypeError)


#: Deterministic edits that auto-apply with NO JEV/DeepSeek call when they are the ONLY changes to a row
#: whose artist field is filled (the maintainer, Fri 2 Oct). Entry needs (1) a full-library structural audit with every
#: firing row checked and (2) agreement with AcoustID/MusicBrainz truth. Audit: 10,138 rows, Fri 2 Oct 2026.
SAFE_TRANSFORMS: tuple[str, ...] = (
    # MIK "8A - 6 - " / "6 - " prefix on a filled artist. Fired 4315, 4292 safe-tier. Failure modes checked
    # on every row: empty remainder 0, a second number prefix 0, same prefix in the filename 0; 41 remainders
    # differ from the filename, all a different version or title in the file name, none a strip error.
    # MusicBrainz: 151/156 exact or base; the 5 differ are MB-side (2 fingerprint collisions, 1 typo, 2 spellings).
    "mik-prefix",
    # MIK "7A (6 - Title)" wrapper. Fired 28, 28 safe-tier. 4/28 lost a ")" before the round 6 balance fix;
    # 0 unbalanced after it. MusicBrainz: 9/9 exact or base.
    "mik-paren",
)
#: Audited and NOT admitted (round 6): key-bpm-suffix (56 fired, bpm matches the stored BPM on every row, but
#: 0 AcoustID/MB rows); bitrate-bracket (2 safe-eligible rows, both with a residual YouTube-style title);
#: whitespace (12 rows, one censored "Ni  as" collapses to a new word); site-prefix, pn-suffix, copy-suffix
#: (0 safe-eligible rows).


def safe_tier(track: Track, proposal: Proposal) -> bool:
    """Every change is a SAFE_TRANSFORMS edit on a filled-artist row and nothing else needs judging."""
    artist = track.artist.strip()
    padded = (
        track.artist != artist or track.title != track.title.strip()
    )  # trimming is an unaudited edit: never auto-applied
    return (
        not padded
        and bool(proposal.transforms)
        and set(proposal.transforms) <= set(SAFE_TRANSFORMS)
        and not proposal.review_reasons
        and bool(artist)
        and proposal.artist == artist
        and not _needs_human(proposal, track.title)
        and not mashup_candidate(track, proposal)
        and "mashup" not in proposal.tags  # an explicit mashup is never judged, so it is never safe
    )


def fix_track(
    track: Track,
    api_key: str | None = None,
    *,
    deadline: float | None = None,
    budget: SpendBudget | None = None,
) -> Verdict:
    """api_key None reads $OPENROUTER_API_KEY; deadline is a time.monotonic() stamp (the run's wall budget);
    budget is the run's shared spend budget, checked before every paid call."""
    api = Api(api_key, deadline, budget)
    proposal = regex_propose(track)
    base = Verdict("unknown", track.artist, track.title, "none", proposal)
    strong, weak = project_rules(track)
    if (no_api := _no_api_verdict(track, proposal, base, strong, weak)) is not None:
        return no_api
    project_note = ""
    if strong or weak:
        try:
            p_project, cost = judge_project_with_jev(track, api)
        except API_ERRORS as exc:
            return replace(base, error=f"jev project: {exc}", cost_usd=base.cost_usd + charged_usd(exc))
        base = replace(base, cost_usd=cost)
        decision = project_decision(strong, weak, p_project)
        if decision == "project":
            note = f"project rules {', '.join(strong or weak)}, JEV p(project) {p_project:.2f}"
            return replace(base, status="project", source="rules+jev", tags=project_tags(strong or weak), notes=[note])
        if decision == "review":
            project_note = f"project rules {', '.join(strong)} fired but JEV p(project) {p_project:.2f}"
    return _demote_to_review(with_tags(_fix_title(track, proposal, base, api)), project_note)


def _no_api_verdict(
    track: Track, proposal: Proposal, base: Verdict, strong: tuple[str, ...], weak: tuple[str, ...]
) -> Verdict | None:
    """The two certain paths that need no JEV/DeepSeek call; None sends the row on to the judges."""
    if "sampler" in strong:  # certain on its own: no JEV (round 6, sampler one-shots sat at JEV 0.73-0.82)
        note = f"project rules {', '.join(strong)} (sampler is certain: no JEV)"
        return replace(base, status="project", source="safe-tier", tags=project_tags(strong), notes=[note])
    if not (strong or weak) and safe_tier(track, proposal):
        status: Status = "clean" if _same(proposal, Proposal(track.artist, track.title)) else "fixed"
        note = f"safe tier: {', '.join(proposal.transforms)}"
        return with_tags(
            replace(base, status=status, artist=proposal.artist, title=proposal.title, source="safe-tier", notes=[note])
        )
    return None


def _demote_to_review(verdict: Verdict, project_note: str) -> Verdict:
    """An auto-applicable verdict goes to review when JEV disputed the project rules or tagged a mashup."""
    if project_note and verdict.status in (
        "fixed",
        "clean",
        "not_music",
    ):  # every usable verdict, unknown stays unknown
        return replace(verdict, status="review", notes=[*verdict.notes, project_note])
    if CFG.MASHUP_REVIEW and _is_mashup(verdict) and verdict.status in ("fixed", "clean"):
        return replace(verdict, status="review", notes=[*verdict.notes, "mashup: review"])
    return verdict


def _jev_mashup(verdict: Verdict) -> bool:
    return verdict.jev_p_mashup is not None and verdict.jev_p_mashup >= CFG.MASHUP_THRESHOLD


def _is_mashup(verdict: Verdict) -> bool:
    """JEV tagged it, or the title itself says mashup (the regex tag, which skips the JEV mashup question)."""
    return _jev_mashup(verdict) or "mashup" in verdict.regex.tags


def with_tags(verdict: Verdict) -> Verdict:
    """Tags follow the FINAL title; row-level tags (cover, provenance) ride along from the regex proposal."""
    if verdict.status in ("not_music", "unknown", "project"):
        return verdict
    regex = verdict.regex
    row_tags = tuple(t for t in regex.tags if t not in version_tags(regex.title))
    mashup = ("mashup",) if _jev_mashup(verdict) else ()
    return replace(
        verdict,
        tags=_merge_tags(version_tags(verdict.title), row_tags, mashup),
        original_artist=regex.original_artist,
        featured_artists=regex.featured_artists,
        producer=regex.producer,
    )


def _fix_title(track: Track, proposal: Proposal, base: Verdict, api: Api) -> Verdict:
    if proposal.review_reasons:
        return replace(
            base,
            status="review",
            artist=proposal.artist,
            title=proposal.title,
            source="regex",
            notes=list(proposal.review_reasons),
        )
    try:
        jev = judge_with_jev(track, proposal, api)
    except API_ERRORS as exc:
        return replace(base, error=f"jev: {exc}", cost_usd=base.cost_usd + charged_usd(exc))
    cost = base.cost_usd + jev.cost
    base = replace(base, jev_kind=jev.kind, jev_p_ok=jev.p_ok, jev_p_mashup=jev.p_mashup, cost_usd=cost)
    unchanged = _same(proposal, Proposal(track.artist, track.title))
    if jev.kind == "not_music":
        return replace(base, status="not_music")
    if jev.p_ok >= _agree_threshold(track, proposal) and not _needs_human(proposal, track.title):
        status: Status = "clean" if unchanged else "fixed"
        return replace(base, status=status, artist=proposal.artist, title=proposal.title, source="regex+jev")
    try:
        llm, llm_cost = extract_with_llm(track, api)
    except API_ERRORS as exc:
        return replace(base, error=f"llm: {exc}", cost_usd=cost + charged_usd(exc))
    return _settle_with_llm(replace(base, llm=llm, cost_usd=cost + llm_cost), track, proposal, llm, unchanged)


def _settle_with_llm(base: Verdict, track: Track, proposal: Proposal, llm: Proposal, unchanged: bool) -> Verdict:
    """DeepSeek alone never auto-applies: it can only confirm the regex or the raw row."""
    if not _artist_in_row(llm.artist, track):
        note = f"llm artist {llm.artist!r} is not in the row"
        return _llm_evidence_only(base, proposal, note)
    if _artist_is_title_head(llm.artist, proposal):
        note = f"llm artist {llm.artist!r} is the head of the song title"
        return _llm_evidence_only(base, proposal, note)
    if reason := _needs_human(llm, track.title):
        return _llm_evidence_only(base, proposal, f"{reason}: llm said {llm.artist!r} - {llm.title!r}")
    if _same(llm, proposal):
        if dropped := dropped_brackets(track.title, proposal.title):
            note = f"drops bracketed text {', '.join(dropped)} that is not on the strip list"
            return replace(
                base,
                status="review",
                artist=proposal.artist,
                title=proposal.title,
                source="regex+deepseek",
                notes=[note],
            )
        status: Status = "clean" if unchanged else "fixed"
        return replace(base, status=status, artist=proposal.artist, title=proposal.title, source="regex+deepseek")
    if _same(llm, Proposal(track.artist, track.title)):
        return replace(base, status="clean", source="deepseek(raw ok)")
    return _llm_evidence_only(base, proposal, f"llm disagrees: {llm.artist!r} - {llm.title!r}")


def _llm_evidence_only(base: Verdict, proposal: Proposal, note: str) -> Verdict:
    """A DeepSeek answer that does not confirm the regex is review EVIDENCE: the proposal a person can select and
    apply stays the regex one, never text the model produced."""
    return replace(base, status="review", artist=proposal.artist, title=proposal.title, source="deepseek", notes=[note])


def dropped_brackets(raw_title: str, title: str) -> list[str]:
    """Bracketed groups of the raw title that are gone from `title` and are not on the strip list (R21)."""
    kept = _fold(title)
    return [
        group
        for group in Rx.BRACKET.findall(raw_title.translate(Rx.FULL_WIDTH))
        if _fold(group) and _fold(group) not in kept and not _is_junk_bracket(group)
    ]


def _is_junk_bracket(group: str) -> bool:
    """Strippable junk, unless the group names a version: "(Official Remix)" is a remix, not a promo label."""
    return bool(Rx.STRIPPABLE_BRACKET.search(group)) and not _names_a_version(group)


def fix_tracks(
    tracks: list[Track],
    api_key: str | None = None,
    *,
    wall_budget_s: float | None = None,
    spend_budget_usd: float | None = None,
    spend_budget: SpendBudget | None = None,
) -> list[Verdict]:
    """api_key None reads $OPENROUTER_API_KEY (CLI/eval). wall_budget_s caps the whole run: once it is spent no
    new OpenRouter call starts and in-flight calls stop at it, so those rows come back unknown with the reason.
    spend_budget_usd caps the run's OpenRouter cost the same way: once it is spent no new call starts.
    spend_budget is a caller-owned SpendBudget shared across several calls of this function (a chunked scan)."""
    key = _api_key(Api(api_key))  # fail before fanning out
    deadline = None if wall_budget_s is None else time.monotonic() + wall_budget_s
    if spend_budget is not None and spend_budget_usd is not None:
        raise ValueError("pass spend_budget_usd or spend_budget, not both")
    budget = (
        spend_budget
        if spend_budget is not None
        else (None if spend_budget_usd is None else SpendBudget(spend_budget_usd))
    )
    with ThreadPoolExecutor(CFG.WORKERS) as pool:
        return list(pool.map(lambda track: fix_track(track, key, deadline=deadline, budget=budget), tracks))


def verdict_row(track: Track, verdict: Verdict) -> dict[str, Any]:
    llm = verdict.llm
    return {
        **asdict(track),
        "regex_artist": verdict.regex.artist,
        "regex_title": verdict.regex.title,
        "mik": " ".join(x for x in (verdict.regex.key, verdict.regex.energy, verdict.regex.bpm) if x),
        "jev_kind": verdict.jev_kind,
        "jev_p_ok": "" if verdict.jev_p_ok is None else round(verdict.jev_p_ok, 2),
        "llm_artist": llm.artist if llm else "",
        "llm_title": llm.title if llm else "",
        "status": verdict.status,
        "source": verdict.source,
        "final_artist": verdict.artist,
        "final_title": verdict.title,
        "original_artist": verdict.original_artist,
        "featured_artists": "; ".join(verdict.featured_artists),
        "producer": verdict.producer,
        "transforms": " ".join(verdict.regex.transforms),
        "jev_p_mashup": "" if verdict.jev_p_mashup is None else round(verdict.jev_p_mashup, 2),
        "llm_called": "Y" if llm else "",
        "tags": " ".join(verdict.tags),
        "notes": "; ".join(verdict.notes),
        "error": verdict.error,
    }
