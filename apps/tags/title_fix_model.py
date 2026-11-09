"""Title-fix data model: CFG, Status, Track, Proposal, Verdict.

Split out of `apps/tags/title_fix.py` (#9686); the mini-PRD for these rules lives in that module's docstring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


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
    length_s: float | None = None
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
