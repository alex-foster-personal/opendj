"""ListenBrainz listen payloads, submit eligibility and an opt-in submit client.

Reimplemented from the public ListenBrainz API docs: a listen is submitted once the
user has heard half the track or four minutes of it, whichever is lower, and a
``single`` submission carries ``listened_at`` plus ``track_metadata`` whose
``artist_name`` and ``track_name`` are required. ``ListenBrainzClient`` is the opt-in
network layer: it does nothing unless the user's settings enable it with a token.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

from apps.shared.settings import ListenBrainzSettings

FOUR_MINUTES_MS: int = 4 * 60 * 1000
LISTEN_TYPE_SINGLE: str = "single"


class ListenPayloadError(ValueError):
    """A play lacks metadata the ListenBrainz API requires."""


@dataclass(frozen=True)
class Play:
    """One play of one track, identified by ``play_id`` for duplicate prevention."""

    play_id: str
    artist: str | None
    title: str | None
    listened_at: int
    duration_ms: int | None = None
    release: str | None = None


# -----------------------------------------------------------------------------
# Eligibility


def submit_threshold_ms(duration_ms: int | None) -> int:
    """Return the played time a listen needs: half the track or four minutes, the lower."""
    if duration_ms is None:
        return FOUR_MINUTES_MS
    elif duration_ms <= 0:
        raise ValueError(f"duration_ms must be positive, got {duration_ms}")
    return min(duration_ms // 2, FOUR_MINUTES_MS)


def is_submit_eligible(play: Play, played_ms: int, submitted_play_ids: Collection[str]) -> bool:
    """Return True when ``play`` has been heard long enough and was not yet submitted."""
    if played_ms < 0:
        raise ValueError(f"played_ms must not be negative, got {played_ms}")
    if play.play_id in submitted_play_ids:
        return False
    return played_ms >= submit_threshold_ms(play.duration_ms)


# -----------------------------------------------------------------------------
# Payload


def _required_text(value: str | None, field: str) -> str:
    if value is None or not value.strip():
        raise ListenPayloadError(f"ListenBrainz listen needs a non-empty {field}")
    return value.strip()


def build_track_metadata(play: Play) -> dict[str, Any]:
    """Return the ``track_metadata`` object for ``play``."""
    metadata: dict[str, Any] = {
        "artist_name": _required_text(play.artist, "artist_name"),
        "track_name": _required_text(play.title, "track_name"),
    }
    if play.release is not None and play.release.strip():
        metadata["release_name"] = play.release.strip()
    if play.duration_ms is not None:
        metadata["additional_info"] = {"duration_ms": play.duration_ms}
    return metadata


def build_single_payload(play: Play) -> dict[str, Any]:
    """Return a documented ``single`` submit-listens body for ``play``."""
    return {
        "listen_type": LISTEN_TYPE_SINGLE,
        "payload": [{"listened_at": play.listened_at, "track_metadata": build_track_metadata(play)}],
    }


# -----------------------------------------------------------------------------
# Opt-in submit client

DEFAULT_API_ROOT: str = "https://api.listenbrainz.org"
SUBMIT_LISTENS_PATH: str = "/1/submit-listens"
SUBMIT_TIMEOUT_S: float = 10.0


class ListenBrainzConfigError(RuntimeError):
    """ListenBrainz is enabled but cannot submit as configured."""


class ListenBrainzSubmitError(RuntimeError):
    """The endpoint refused a submit; the play stays pending for a retry."""


@dataclass
class ListenBrainzClient:
    """Submit eligible plays to ListenBrainz when the user's settings enable it."""

    settings: ListenBrainzSettings
    api_root: str = DEFAULT_API_ROOT
    submitted_play_ids: set[str] = field(default_factory=set)

    def submit(self, play: Play, played_ms: int) -> bool:
        """Submit ``play`` if enabled and eligible; return True only when the endpoint accepted it."""
        if not self.settings.enabled:
            return False
        elif self.settings.token is None:
            raise ListenBrainzConfigError("ListenBrainz is enabled but settings have no token")
        if not is_submit_eligible(play, played_ms, self.submitted_play_ids):
            return False
        self._post(build_single_payload(play), self.settings.token)
        self.submitted_play_ids.add(play.play_id)
        return True

    def _post(self, body: dict[str, Any], token: str) -> None:
        request = urllib.request.Request(
            self.api_root.rstrip("/") + SUBMIT_LISTENS_PATH,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Authorization": f"Token {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=SUBMIT_TIMEOUT_S) as response:
                status = response.status
        except urllib.error.HTTPError as error:
            raise ListenBrainzSubmitError(f"ListenBrainz refused the submit: HTTP {error.code}") from error
        if not 200 <= status < 300:
            raise ListenBrainzSubmitError(f"ListenBrainz refused the submit: HTTP {status}")
