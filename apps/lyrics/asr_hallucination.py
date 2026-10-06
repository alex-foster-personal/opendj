"""Reject Whisper hallucinations in line-level ASR lyrics (LYRICS-12).

ASR runs on the separated vocal stem. On an instrumental that stem is near
silence, and Whisper fills silence with lines it learned from subtitle
corpora: "Thank you.", "Thanks for watching!", "(c) transcript Emily
Beynon", "Teksting av Nicolai Winther", or a bare ".". In a real library a
large share of ASR lyric caches hold at least one "Thank you." line, and an
instrumental track can render a dozen of them on a deck.

Line classes, decided on normalized text only:

* ``noise``: nothing left once punctuation and symbols are stripped (".",
  a lone music note). Never a lyric.
* ``signature``: a phrase that only exists as a subtitle/video artefact
  (credits, "for watching", "subscribe", "(c)"). Never a lyric, whatever
  the count.
* ``filler``: a short phrase a real song CAN sing ("thank you", "bye",
  "you") but that Whisper also emits over silence. Dropped only when the
  same filler is a whole line ``FILLER_REPEAT_MIN`` or more times, so one
  sung "Thank you." among real lines survives.
* ``interjection``: only "oh", "yeah" and the like. Always kept, since a
  real vocal track sings ad-libs, but never evidence of lyrics by itself.

Signatures and fillers cover Whisper's boilerplate in many languages
(LYRICS-13: "Finesse" served "اشتركوا في القناة", Arabic for "subscribe to the
channel", seven times). One language-agnostic rule backs the lists up: a
``lyric`` line repeated in silence and written in a different script from the
rest of the transcript is dropped too (``foreign_script_repeats``).

What remains is a lyric timeline only when it holds at least
``MIN_LYRIC_LINES`` ``lyric`` lines (not filler, not interjection). Below that the transcript is
classified no-lyrics: the fetch path records the existing ``instrumental``
verdict instead of caching it, and the read route answers 404 so the deck
shows NO LYRIC DATA. Cached files are never deleted or rewritten; they are
filtered on read, by every reader, so the surfaces agree:
``GET /tracks/{id}/lyrics``, ``lyrics_available``, ``/tracks/lyrics-cached-ids``
(``valid_lyrics_ids``), coverage (terminal, like ``instrumental``), the lyric
search index (schema v3 rebuild) and its snippets.
"""

from __future__ import annotations

import re
import statistics
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from apps.lyrics import cache
from apps.lyrics.cache import LyricLine, Lyrics
from apps.lyrics.tail_sanitize import HALLUCINATION_PHRASES

ASR_SOURCE = "asr"

#: A filler phrase repeated as a whole line this many times is hallucinated fill.
FILLER_REPEAT_MIN: int = 2

#: Fewer non-filler lines than this after filtering means the track has no lyrics.
MIN_LYRIC_LINES: int = 2

#: Short phrases a song may sing but Whisper also emits over silence (normalized).
FILLER_PHRASES: frozenset[str] = frozenset(
    {
        *HALLUCINATION_PHRASES,
        "you",
        "bye",
        "bye bye",
        "goodbye",
        "thank you bye",
        "gracias",
        "amen",
        "tchau",
        "hvala vam",
        "merci",
        "danke",
        "спасибо",
        "i dont know",
        "oh my god",
        "my god",
        "okay",
        "all right",
        "i love you",
        "god bless you",
        "youre welcome",
        # LYRICS-13: "thank you" and "good night" over silence in other languages.
        # Matched against the normalized line or, for CJK, its spaceless form.
        "شكرا",
        "obrigado",
        "obrigada",
        "grazie",
        "terima kasih",
        "teşekkürler",
        "dziękuję",
        "감사합니다",
        "ありがとう",
        "ありがとうございました",
        "おやすみなさい",
        "谢谢",
    }
)

#: A whole line repeated this many times, isolated in silence and in a different
#: script from the rest of the transcript, is hallucinated (LYRICS-13).
FOREIGN_REPEAT_MIN: int = 2

#: Median gap, in seconds, from each occurrence to its nearest neighbouring line.
FOREIGN_REPEAT_ISOLATION_S: float = 10.0

#: Lines in other scripts needed before a dominant script can be named.
FOREIGN_REPEAT_CONTEXT_MIN: int = 2

#: Unicode script names folded together: Japanese mixes kana with CJK ideographs.
_SCRIPT_GROUPS: dict[str, str] = {"HIRAGANA": "CJK", "KATAKANA": "CJK"}

#: Words that make a line a bare interjection ("Oh, oh, oh.") when it holds
#: nothing else: always kept, never counted as a lyric line on its own.
INTERJECTIONS: frozenset[str] = frozenset(
    {"oh", "ooh", "ah", "uh", "um", "yeah", "hey", "yes", "no"}
)

#: Subtitle and video artefacts. Matched against normalized text (casefolded,
#: punctuation removed, apostrophes dropped) or, for CJK, its spaceless form.
_SIGNATURE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        r"\b(thanks|thank you)( so much| very much)? for (watching|listening|viewing)\b",
        r"\bsubtitles?\b",
        r"\bsubt[ií]tulos\b",
        r"\bsottotitoli\b",
        r"\buntertitel",
        r"субтитр",
        r"продолжение следует",
        r"\btranscri(pt|ption|bed)\b",
        r"\bteksting\b",
        r"\btekstet\b",
        r"\btakk for\b.*\bmed\b",
        r"\btakk for at du så\b",
        r"\bsous titrage\b",
        r"\bnorsk tekst",
        r"\bgracias por ver\b",
        r"\bend of the video\b",
        r"\bamara\b.*\borg\b",
        r"\bsubscribe\b",
        r"\bsuscr[ií]bete\b",
        r"\bpratite\b.*\bkanal\b",
        r"\bmooji\b",
        r"\bwell be right back\b",
        r"\bsee you (in the )?next (time|video|episode)\b",
        r"\bbf watch tv\b",
        r"\bsee you next week\b",
        r"\bundertexter\b",
        r"\bdaj se muzici\b",
        r"^the end$",
        r"^конец$",
        r"играет музыка",
        r"^(outro |intro )?musi[ck]$",
        r"^choir singing$",
        r"ご視聴",
        # LYRICS-13: Whisper's YouTube-outro boilerplate in other languages.
        # Sources: Baranski et al., "Investigation of Whisper ASR Hallucinations
        # Induced by Non-Speech Audio", ICASSP 2025 (arXiv:2501.11378, its "bag
        # of hallucinations"); the sachaarbonel/whisper-hallucinations dataset
        # (huggingface.co/datasets/sachaarbonel/whisper-hallucinations, ~100
        # languages of noise-only output); the Russian list at
        # gist.github.com/waveletdeboshir/8bf52f04bf78018194f25b2390c08309;
        # github.com/openai/whisper/discussions/1873 ("Share your
        # hallucinations"). Plus lines observed recurring verbatim across
        # 3+ unrelated tracks' ASR
        # ("altyazı m k", "legenda adriana zanotto", "한글자막 by 한효정",
        # "terima kasih telah menonton", "izlediğiniz için teşekkür ederim").
        # Arabic: subscribe to the channel, thanks for watching, a subtitler credit.
        r"اشتركوا",
        r"اشترك في القناة",
        r"شكرا (على|لكم على) المشاهدة",
        r"شكرا للمشاهدة",
        r"نانسي قنقر",
        # Spanish / Portuguese: subscribe, thanks for watching, subtitle credits.
        r"\bsuscrib",
        r"\binscrevase\b",
        r"\binscreva se\b",
        r"\bobrigad[oa] por assistir\b",
        r"\blegendas? (pela|por)\b",
        r"\badriana zanotto\b",
        # Russian: subscribe, thanks for viewing.
        r"подписывайтесь",
        r"спасибо за просмотр",
        # Korean: subscribe, thanks for watching, subtitles.
        r"구독",
        r"시청해\s?주셔서",
        r"자막",
        # Japanese: channel subscription, thanks for watching.
        r"チャンネル登録",
        r"見てくれてありがとう",
        r"^お疲れ様でした$",
        # Chinese: thanks for watching, subscribe, like, subtitles, the Mingjing sign-off.
        r"谢谢(大家)?(观看|收看)",
        r"订阅",
        r"点赞",
        r"字幕",
        r"明镜",
        # French / German / Italian / Dutch / Polish.
        r"\bmerci d ?avoir regard",
        r"\babonnez vous\b",
        r"\bsous titres?\b",
        r"\bdanke f[üu]rs zuschauen\b",
        r"\babonniert\b",
        r"\bgrazie per (la visione|aver guardato)\b",
        r"\biscriviti\b",
        r"\bondertitel",
        r"\bbedankt voor het kijken\b",
        r"\bnapisy\b",
        r"\bsubskrybuj",
        # Turkish: subtitles, thanks for watching, subscribe, the "God keep you" sign-off.
        r"\baltyaz",
        r"zlediğiniz için teşekkür",
        r"\babone ol",
        r"\ballah ?a emanet\b",
        # Hindi / Indonesian / Vietnamese / Norwegian.
        r"सब्सक्राइब",
        r"देखने के लिए धन्यवाद",
        r"\bterima kasih (telah|sudah) menonton\b",
        r"\bcảm ơn các bạn đã (xem|theo dõi)\b",
        r"\btakk for watching\b",
        # A bare "[music]" caption, in other languages.
        r"^(m[uú]sica|musique|музыка|موسيقى|音楽|音乐|음악)$",
    )
)

_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")

LineClass = Literal["noise", "signature", "filler", "interjection", "lyric"]
DropReason = Literal["noise", "signature", "repeated-filler", "foreign-script-repeat"]


@dataclass(frozen=True)
class AsrLineFilter:
    """The outcome of filtering one ASR lyric timeline."""

    kept: tuple[LyricLine, ...]
    dropped: tuple[tuple[LyricLine, DropReason], ...]
    lyric_line_count: int

    @property
    def no_lyrics(self) -> bool:
        """True when too few real lyric lines survive: the track has no lyrics."""
        return self.lyric_line_count < MIN_LYRIC_LINES


def normalize_line(text: str) -> str:
    """Casefold, drop apostrophes, strip punctuation and symbols, squash spaces."""
    folded = unicodedata.normalize("NFKC", text).casefold().replace("'", "").replace("’", "")
    return _SPACES.sub(" ", _NON_WORD.sub(" ", folded).replace("_", " ")).strip()


def classify_line(text: str) -> LineClass:
    """Classify one ASR line by its text alone."""
    if "©" in text:
        return "signature"
    normalized = normalize_line(text)
    if not normalized:
        return "noise"
    spaceless = normalized.replace(" ", "")
    # Folded raw text too: normalizing strips combining vowel signs (Devanagari).
    folded = unicodedata.normalize("NFKC", text).casefold()
    probes = (normalized, spaceless, folded)
    if any(p.search(probe) for p in _SIGNATURE_PATTERNS for probe in probes):
        return "signature"
    if normalized in FILLER_PHRASES or spaceless in FILLER_PHRASES:
        return "filler"
    if set(normalized.split()) <= INTERJECTIONS:
        return "interjection"
    return "lyric"


def _dominant_script(text: str) -> str | None:
    """The Unicode script most of ``text``'s letters are written in, or None."""
    scripts: Counter[str] = Counter()
    for char in text:
        if char.isalpha():
            script = unicodedata.name(char, "").split(" ", 1)[0]
            if script:
                scripts[_SCRIPT_GROUPS.get(script, script)] += 1
    return scripts.most_common(1)[0][0] if scripts else None


def _median_isolation_s(starts_ms: Sequence[int], indexes: Sequence[int]) -> float:
    """Median gap, in seconds, from each indexed line to its nearest neighbour."""
    gaps: list[float] = []
    for i in indexes:
        neighbours = [
            abs(starts_ms[j] - starts_ms[i]) for j in (i - 1, i + 1) if 0 <= j < len(starts_ms)
        ]
        gaps.append(min(neighbours) / 1000 if neighbours else 0.0)
    return statistics.median(gaps)


def foreign_script_repeats(lines: Sequence[LyricLine]) -> frozenset[str]:
    """Normalized lines that read as Whisper fill whatever their language (LYRICS-13).

    A line qualifies when all three hold: it repeats as a whole line at least
    ``FOREIGN_REPEAT_MIN`` times; its occurrences sit in silence (median gap to
    the nearest line at least ``FOREIGN_REPEAT_ISOLATION_S``); and it is written
    in a different script from the rest of the transcript. Repetition plus
    isolation alone is not enough: measured on 574 human-authored LRCLIB
    timelines, sparse real hooks ("I'm losing it", "Consciousness") repeat
    every ~30 s across the whole track exactly like fill does.
    """
    ordered = sorted(lines, key=lambda line: line.start_ms)
    normalized = [normalize_line(line.text) for line in ordered]
    starts = [line.start_ms for line in ordered]
    repeats: set[str] = set()
    for text, count in Counter(n for n in normalized if n).items():
        if count < FOREIGN_REPEAT_MIN:
            continue
        indexes = [i for i, n in enumerate(normalized) if n == text]
        if _median_isolation_s(starts, indexes) < FOREIGN_REPEAT_ISOLATION_S:
            continue
        context = Counter(
            script for n in normalized if n != text and (script := _dominant_script(n))
        )
        if sum(context.values()) < FOREIGN_REPEAT_CONTEXT_MIN:
            continue
        if context.most_common(1)[0][0] != _dominant_script(text):
            repeats.add(text)
    return frozenset(repeats)


def filter_asr_lines(lines: Sequence[LyricLine]) -> AsrLineFilter:
    """Drop hallucinated lines and say whether real lyrics remain."""
    classes = [classify_line(line.text) for line in lines]
    filler_counts = Counter(
        normalize_line(line.text) for line, cls in zip(lines, classes) if cls == "filler"
    )
    survivors: list[tuple[LyricLine, LineClass]] = []
    dropped: list[tuple[LyricLine, DropReason]] = []
    for line, cls in zip(lines, classes):
        if cls == "noise":
            dropped.append((line, "noise"))
        elif cls == "signature":
            dropped.append((line, "signature"))
        elif cls == "filler" and filler_counts[normalize_line(line.text)] >= FILLER_REPEAT_MIN:
            dropped.append((line, "repeated-filler"))
        elif cls == "filler" or cls == "interjection" or cls == "lyric":
            survivors.append((line, cls))
        else:
            raise AssertionError(f"unhandled ASR line class {cls!r}")
    foreign = foreign_script_repeats([line for line, cls in survivors if cls == "lyric"])
    kept: list[LyricLine] = []
    lyric_lines = 0
    for line, cls in survivors:
        if cls == "lyric" and normalize_line(line.text) in foreign:
            dropped.append((line, "foreign-script-repeat"))
        elif cls == "lyric":
            kept.append(line)
            lyric_lines += 1
        elif cls == "filler" or cls == "interjection":
            kept.append(line)
        else:
            raise AssertionError(f"unhandled ASR line class {cls!r}")
    return AsrLineFilter(tuple(kept), tuple(dropped), lyric_lines)


def servable_lyrics(lyrics: Lyrics) -> Lyrics | None:
    """Cached lyrics as they may be shown: ASR filtered, None when it has no lyrics.

    Non-ASR sources (LRCLIB, licensed providers) are human-authored and pass
    through untouched. The cache file itself is never rewritten.
    """
    if lyrics.source != ASR_SOURCE:
        return lyrics
    asr_filter = filter_asr_lines(lyrics.lines)
    if asr_filter.no_lyrics:
        return None
    return Lyrics(lyrics.stable_id, lyrics.source, asr_filter.kept)


def cached_entry_is_no_lyrics(data_dir: Path, stable_id: str) -> bool:
    """True when ``stable_id`` has a readable cache entry that serves as no-lyrics.

    A missing or corrupt entry is False: those are "missing" and "corrupt",
    which their own readers report; this answers only "cached, but empty once
    the hallucinations are gone".
    """
    try:
        entry = cache.load(cache.cache_path(data_dir, stable_id))
    except (TypeError, ValueError):
        return False
    return entry is not None and entry.stable_id == stable_id and servable_lyrics(entry) is None


__all__ = [
    "ASR_SOURCE",
    "FILLER_PHRASES",
    "FILLER_REPEAT_MIN",
    "FOREIGN_REPEAT_ISOLATION_S",
    "FOREIGN_REPEAT_MIN",
    "MIN_LYRIC_LINES",
    "AsrLineFilter",
    "cached_entry_is_no_lyrics",
    "classify_line",
    "filter_asr_lines",
    "foreign_script_repeats",
    "normalize_line",
    "servable_lyrics",
]
