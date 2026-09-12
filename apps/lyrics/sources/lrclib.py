"""LRCLIB provider (tier: free). https://lrclib.net -- keyless, duration-anchored."""

from __future__ import annotations

from apps.lyrics.sources.base import CheckResult, Track, http_get_json

BASE = "https://lrclib.net/api"


class LrclibProvider:
    name = "lrclib"
    tier = "free"
    rate_delay_s = 0.3
    daily_budget: int | None = None

    def _get(self, track: Track) -> dict | None:
        status, body = http_get_json(f"{BASE}/get", {
            "artist_name": track.artist,
            "track_name": track.norm_title,
            "duration": str(track.duration_s),
        })
        return body if status == 200 and isinstance(body, dict) else None

    def check(self, track: Track) -> CheckResult:
        exact = self._get(track)
        if exact is not None:
            return CheckResult(
                found=bool(exact.get("plainLyrics") or exact.get("syncedLyrics")),
                synced=bool(exact.get("syncedLyrics")),
                instrumental=bool(exact.get("instrumental")),
                detail="exact",
            )
        _, results = http_get_json(f"{BASE}/search", {
            "track_name": track.norm_title, "artist_name": track.primary_artist,
        })
        hits = [r for r in (results or []) if r.get("plainLyrics") or r.get("syncedLyrics")]
        return CheckResult(
            found=bool(hits),
            synced=any(r.get("syncedLyrics") for r in hits),
            detail=f"search:{len(hits)}",
        )

    def fetch(self, track: Track) -> str | None:
        return self.fetch_pair(track)[0]

    def fetch_pair(self, track: Track) -> tuple[str | None, str | None]:
        """(plain, synced LRC) in ONE API call. Synced is stored alongside as
        a timing prior for the version-verifier thread, never a timing source
        of truth -- the aligner owns word timings."""
        exact = self._get(track)
        if exact is None:
            return None, None
        return exact.get("plainLyrics") or None, exact.get("syncedLyrics") or None
