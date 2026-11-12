"""AcoustID and MusicBrainz lookup for untagged library rows (issue #10429).

AcoustID: https://acoustid.org/webservice
MusicBrainz recording: https://musicbrainz.org/doc/MusicBrainz_API
MusicBrainz release labels: same API with ``inc=labels``.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urljoin

import httpx

from apps.shared.app_version import APP_VERSION

ACOUSTID_URL = "https://api.acoustid.org/v2/lookup"
MB_URL = "https://musicbrainz.org/ws/2/"
KEY_ENV = "ODJ_ACOUSTID_KEY"
SCORE_FLOOR = 0.9
SPACING_S = 1.0
BACKOFF_S = 60.0
TIMEOUT_S = 10.0
MAX_CANDIDATES = 3
CONTACT_URL = "https://github.com/private_owner/music-dj-tools"

__all__ = [
    "ACOUSTID_URL",
    "BACKOFF_S",
    "KEY_ENV",
    "MB_URL",
    "SCORE_FLOOR",
    "SPACING_S",
    "TIMEOUT_S",
    "Candidate",
    "IdentifyDeferred",
    "IdentifyError",
    "IdentifyProposal",
    "api_key",
    "identify",
    "reset_for_tests",
    "user_agent",
]


@dataclass(frozen=True)
class Candidate:
    recording_id: str
    artist: str
    title: str
    year: int | None
    label: str | None


@dataclass(frozen=True)
class IdentifyProposal:
    status: Literal["confident", "needs_review"]
    score: float
    best: Candidate
    candidates: tuple[Candidate, ...]


class IdentifyDeferred(Exception):
    """AcoustID or MusicBrainz is busy or unreachable; defer the rest of the batch."""


class IdentifyError(Exception):
    """A non-retryable lookup failure for one row."""


class _Throttle:
    def __init__(
        self, *, clock: Callable[[], float] | None = None, sleep: Callable[[float], None] | None = None
    ) -> None:
        self._lock = threading.Lock()
        self._last = 0.0
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            wait = self._last + SPACING_S - now
            if wait > 0:
                self._sleep(wait)
            self._last = self._clock()


_THROTTLE = _Throttle()


class _Backoff:
    __slots__ = ("lock", "until")

    def __init__(self) -> None:
        self.until = 0.0
        self.lock = threading.Lock()


_BACKOFF = _Backoff()


def api_key() -> str | None:
    raw = os.environ.get(KEY_ENV, "")
    stripped = raw.strip()
    return stripped or None


def user_agent() -> str:
    return f"OpenDJ/{APP_VERSION} ({CONTACT_URL})"


def reset_for_tests() -> None:
    with _BACKOFF.lock:
        _BACKOFF.until = 0.0
    _THROTTLE._last = 0.0


def _check_backoff(clock: Callable[[], float]) -> None:
    with _BACKOFF.lock:
        until = _BACKOFF.until
    if until and clock() < until:
        raise IdentifyDeferred("AcoustID / MusicBrainz busy (HTTP 503); press Retry to look it up again")


def _set_backoff(clock: Callable[[], float]) -> None:
    with _BACKOFF.lock:
        _BACKOFF.until = clock() + BACKOFF_S


def _get(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, str] | None = None,
    throttle: _Throttle,
    clock: Callable[[], float],
) -> httpx.Response:
    _check_backoff(clock)
    throttle.wait()
    response = client.get(
        url,
        params=params,
        headers={"User-Agent": user_agent(), "Accept": "application/json"},
        timeout=TIMEOUT_S,
    )
    if response.status_code == 503:
        _set_backoff(clock)
        raise IdentifyDeferred("AcoustID / MusicBrainz busy (HTTP 503); press Retry to look it up again")
    if response.status_code >= 400:
        raise IdentifyError(f"HTTP {response.status_code} from {url}")
    return response


def _artist_credit(payload: dict) -> str:
    parts: list[str] = []
    for credit in payload.get("artist-credit", []):
        name = credit.get("name") or credit.get("artist", {}).get("name") or ""
        join = credit.get("joinphrase", "")
        parts.append(f"{name}{join}")
    return "".join(parts).strip()


def _year_from_date(date_str: str | None) -> int | None:
    if not date_str or len(date_str) < 4:
        return None
    try:
        return int(date_str[:4])
    except ValueError:
        return None


def _response_json(resp: httpx.Response, *, label: str) -> dict:
    try:
        data = resp.json()
    except ValueError as exc:
        raise IdentifyError(f"invalid JSON from {label}") from exc
    if not isinstance(data, dict):
        raise IdentifyError(f"unexpected JSON from {label}")
    return data


def _fetch_recording(client: httpx.Client, mbid: str, throttle: _Throttle, clock: Callable[[], float]) -> Candidate:
    url = urljoin(MB_URL, f"recording/{mbid}")
    resp = _get(client, url, params={"inc": "artist-credits+releases", "fmt": "json"}, throttle=throttle, clock=clock)
    data = _response_json(resp, label=url)
    artist = _artist_credit(data)
    title = data.get("title") or ""
    releases = data.get("releases") or []
    year: int | None = None
    release_id: str | None = None
    if releases:
        dated = sorted(
            (r for r in releases if r.get("date") or r.get("first-release-date")),
            key=lambda r: r.get("date") or r.get("first-release-date") or "9999",
        )
        pick = dated[0] if dated else releases[0]
        year = _year_from_date(pick.get("date") or pick.get("first-release-date"))
        release_id = pick.get("id")
    label: str | None = None
    if release_id:
        rel_url = urljoin(MB_URL, f"release/{release_id}")
        rel_resp = _get(client, rel_url, params={"inc": "labels", "fmt": "json"}, throttle=throttle, clock=clock)
        rel_data = _response_json(rel_resp, label=rel_url)
        label_infos = rel_data.get("label-info") or []
        if label_infos:
            label = (label_infos[0].get("label") or {}).get("name")
    return Candidate(recording_id=mbid, artist=artist, title=title, year=year, label=label)


def _fetch_recording_basic(
    client: httpx.Client, mbid: str, throttle: _Throttle, clock: Callable[[], float]
) -> Candidate:
    url = urljoin(MB_URL, f"recording/{mbid}")
    resp = _get(client, url, params={"inc": "artist-credits", "fmt": "json"}, throttle=throttle, clock=clock)
    data = _response_json(resp, label=url)
    return Candidate(
        recording_id=mbid,
        artist=_artist_credit(data),
        title=data.get("title") or "",
        year=_year_from_date(data.get("first-release-date")),
        label=None,
    )


def identify(
    fingerprint: str,
    duration_s: float,
    *,
    client: httpx.Client | None = None,
    key: str | None = None,
    throttle: _Throttle | None = None,
    clock: Callable[[], float] | None = None,
) -> IdentifyProposal | None:
    resolved_key = api_key() if key is None else key
    if not resolved_key:
        raise IdentifyError("ODJ_ACOUSTID_KEY not set")
    th = throttle or _THROTTLE
    clk = clock or time.monotonic
    own_client = client is None
    http = client or httpx.Client()
    try:
        params = {
            "client": resolved_key,
            "duration": f"{round(duration_s):.0f}",
            "fingerprint": fingerprint,
            "meta": "recordingids",
            "format": "json",
        }
        resp = _get(http, ACOUSTID_URL, params=params, throttle=th, clock=clk)
        body = _response_json(resp, label=ACOUSTID_URL)
        if body.get("status") != "ok":
            raise IdentifyError(f"AcoustID status {body.get('status')!r}")
        results = body.get("results") or []
        if not results:
            return None
        best = max(results, key=lambda row: float(row.get("score") or 0))
        score = float(best.get("score") or 0)
        recordings = best.get("recordings") or []
        if not recordings:
            return None
        if score >= SCORE_FLOOR and len(recordings) == 1:
            first = recordings[0]
            if not isinstance(first, dict):
                raise IdentifyError("AcoustID response missing recording id")
            mbid = first.get("id")
            if not isinstance(mbid, str) or not mbid:
                raise IdentifyError("AcoustID response missing recording id")
            candidate = _fetch_recording(http, mbid, th, clk)
            return IdentifyProposal(status="confident", score=score, best=candidate, candidates=(candidate,))
        candidates = [
            _fetch_recording_basic(http, rec["id"], th, clk)
            for rec in recordings[:MAX_CANDIDATES]
            if isinstance(rec, dict) and isinstance(rec.get("id"), str) and rec.get("id")
        ]
        if not candidates:
            return None
        return IdentifyProposal(
            status="needs_review",
            score=score,
            best=candidates[0],
            candidates=tuple(candidates),
        )
    except httpx.TransportError as exc:
        _set_backoff(clk)
        raise IdentifyDeferred("AcoustID / MusicBrainz busy (HTTP 503); press Retry to look it up again") from exc
    finally:
        if own_client:
            http.close()
