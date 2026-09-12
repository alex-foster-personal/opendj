"""Provider framework for lyric sourcing: check availability, fetch text.

One tool, many providers (specs/lyric-sourcing.md verdict). Each provider
declares a tier: 'free' (no key, no hard quota), 'keyed' (free key, hard daily
quota), 'paid' (commercial license). The CLI filters by tier so free sweeps
and budgeted keyed drips share one codebase instead of diverging scripts.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from apps.shared.paths import STATE_DIR as STATE_DIR_SHARED

STATE_DIR = STATE_DIR_SHARED / "lyrics-eval"
USER_AGENT = "music-dj-tools-lyric-sources/0.1 (personal library tooling)"

#-----------------------------------------------------------------------------


@dataclass(frozen=True)
class Track:
    artist: str
    title: str
    norm_title: str
    primary_artist: str
    duration_s: int
    audio_path: str | None  # None = no resolvable local audio

    @property
    def key(self) -> str:
        raw = f"{self.artist}|{self.title}|{self.duration_s}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class CheckResult:
    found: bool
    synced: bool = False
    instrumental: bool = False
    detail: str = ""


class Provider(Protocol):
    name: str
    tier: str  # 'free' | 'keyed' | 'paid'
    rate_delay_s: float
    daily_budget: int | None  # None = no hard quota

    def check(self, track: Track) -> CheckResult: ...
    def fetch(self, track: Track) -> str | None: ...


@dataclass
class Budget:
    """Persisted per-provider daily call counter; hard-stops at the quota so a
    sweep can never burn past an official limit."""

    provider: str
    daily_budget: int | None
    path: Path = field(init=False)
    calls_today: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self.path = STATE_DIR / "source-availability" / f"{self.provider}.budget.json"
        today = datetime.now(UTC).date().isoformat()
        if self.path.is_file():
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            self.calls_today = saved["calls"] if saved["date"] == today else 0

    def spend(self, n: int = 1) -> None:
        if self.daily_budget is not None and self.calls_today + n > self.daily_budget:
            raise BudgetExhausted(
                f"{self.provider}: daily budget {self.daily_budget} reached "
                f"({self.calls_today} spent) -- resume tomorrow, the ledger is resumable"
            )
        self.calls_today += n
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(
            {"date": datetime.now(UTC).date().isoformat(), "calls": self.calls_today}
        ), encoding="utf-8")


class BudgetExhausted(RuntimeError):
    pass


# lrclib 503 storms killed the batch-2 AND batch-3a candidate stages mid-run;
# transient 5xx gets a bounded backoff, then fails loudly (stage is resumable).
RETRY_ATTEMPTS = 5
RETRY_BASE_DELAY_S = 2.0


def http_get_json(url: str, params: dict[str, str],
                  headers: dict[str, str] | None = None) -> tuple[int, dict | list | None]:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full, headers={"User-Agent": USER_AGENT, **(headers or {})})
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 404, None
            if e.code < 500 or attempt == RETRY_ATTEMPTS:
                raise
            delay = RETRY_BASE_DELAY_S * 2 ** (attempt - 1)
            print(f"[WARN] {e.code} from {url} - retry {attempt}/{RETRY_ATTEMPTS - 1} "
                  f"in {delay:.0f}s")
            time.sleep(delay)
    raise AssertionError("unreachable: loop returns or raises")


def polite_sleep(provider: Provider) -> None:
    time.sleep(provider.rate_delay_s)
