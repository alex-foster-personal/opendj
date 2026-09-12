"""Musixmatch provider (tier: keyed). Plan on file: Lyrics API Basic v2,
$49/mo, 5,000 hits/day, app "the maintainer's App" -- PROBED Fri 28 Aug 2026:
matcher.lyrics.get returns FULL lyrics (no truncation disclaimer);
matcher.subtitle.get is 403 (no synced text on Basic; LRCLIB covers LRC
priors, the aligner makes word timings anyway).

Key: env MUSIXMATCH_API_KEY (store in Doppler general/dev_personal; run via
`doppler run -- ...`). Fails fast if absent. Budget ledger hard-stops at the
daily quota so a sweep can never overrun the plan.
"""

from __future__ import annotations

import os

from apps.lyrics.sources.base import Budget, CheckResult, Track, http_get_json

BASE = "https://api.musixmatch.com/ws/1.1"
DAILY_BUDGET = 4800  # under the Basic v2 5,000/day limit, headroom for manual calls


class MusixmatchProvider:
    name = "musixmatch"
    tier = "keyed"
    rate_delay_s = 0.6
    daily_budget: int | None = DAILY_BUDGET

    def __init__(self) -> None:
        self._key = os.environ.get("MUSIXMATCH_API_KEY", "")
        if not self._key:
            raise SystemExit(
                "[ERROR] MUSIXMATCH_API_KEY not set -- store the key in Doppler "
                "(general/dev_personal) and run via `doppler run -- ...`"
            )
        self._budget = Budget(self.name, self.daily_budget)

    def _call(self, endpoint: str, params: dict[str, str]) -> dict | None:
        self._budget.spend()
        status, body = http_get_json(f"{BASE}/{endpoint}", {**params, "apikey": self._key})
        if status != 200 or not isinstance(body, dict):
            return None
        message = body.get("message", {})
        code = message.get("header", {}).get("status_code")
        if code == 401:
            raise SystemExit("[ERROR] musixmatch rejected the API key (401)")
        elif code == 402:
            raise SystemExit("[ERROR] musixmatch daily quota exhausted server-side (402)")
        elif code != 200:
            return None
        return message.get("body") or None

    def check(self, track: Track) -> CheckResult:
        body = self._call("matcher.track.get", {
            "q_artist": track.primary_artist, "q_track": track.norm_title,
        })
        if body is None or "track" not in body:
            return CheckResult(found=False, detail="no-match")
        t = body["track"]
        return CheckResult(
            found=bool(t.get("has_lyrics")),
            synced=bool(t.get("has_subtitles")),
            instrumental=bool(t.get("instrumental")),
            detail=f"track_id:{t.get('track_id')}",
        )

    def fetch(self, track: Track) -> str | None:
        """FULL lyrics on the Basic v2 plan (probed Fri 28 Aug 2026)."""
        body = self._call("matcher.lyrics.get", {
            "q_artist": track.primary_artist, "q_track": track.norm_title,
        })
        if body is None or "lyrics" not in body:
            return None
        text = (body["lyrics"].get("lyrics_body") or "").strip()
        return text or None
