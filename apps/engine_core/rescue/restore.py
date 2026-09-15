"""Pure age-window and outcome helpers shared by HTTP, CLI, and tests."""

from __future__ import annotations

from typing import Any, Literal

RESCUE_PLAY_WINDOW_MS = 10 * 60 * 1000
RESCUE_LAYOUT_WINDOW_MS = 24 * 60 * 60 * 1000

RestoreMode = Literal["layout", "play"]


class RescueRestoreError(Exception):
    """Refused restore: outside window, empty ring, or unknown snapshot."""

    def __init__(self, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


def snapshot_age_ms(*, captured_at_ms: int, now_ms: int) -> int:
    age = now_ms - captured_at_ms
    if age < 0:
        return 0
    return age


def resolve_restore_mode(*, age_ms: int, play: bool) -> RestoreMode:
    if age_ms > RESCUE_LAYOUT_WINDOW_MS:
        raise RescueRestoreError(
            f"newest snapshot is older than the 24 h layout window ({age_ms} ms)",
            status_code=422,
        )
    if play:
        if age_ms > RESCUE_PLAY_WINDOW_MS:
            raise RescueRestoreError(
                f"play restore refused outside the 10 min play window ({age_ms} ms > "
                f"{RESCUE_PLAY_WINDOW_MS} ms)",
                status_code=422,
            )
        return "play"
    return "layout"


def _deck_payload(payload: dict[str, Any], deck_id: str) -> dict[str, Any] | None:
    decks = payload.get("decks")
    if not isinstance(decks, dict):
        return None
    raw = decks.get(deck_id)
    if raw is None:
        raw = decks.get(int(deck_id))
    return raw if isinstance(raw, dict) else None


def compute_deck_outcomes(
    payload: dict[str, Any],
    *,
    mode: RestoreMode,
    present_stable_ids: set[str] | None = None,
) -> dict[str, dict[str, str | None]]:
    """Plan per-deck outcomes for a restore response.

    ``present_stable_ids`` when supplied marks tracks absent from the library as
    ``missing``; when omitted every non-empty stable_id is treated as loadable.
    """
    outcomes: dict[str, dict[str, str | None]] = {}
    for deck_id in ("1", "2", "3", "4"):
        deck = _deck_payload(payload, deck_id)
        stable_id = deck.get("stable_id") if deck else None
        if not isinstance(stable_id, str) or stable_id == "":
            outcomes[deck_id] = {"outcome": "paused", "stable_id": None}
            continue
        if present_stable_ids is not None and stable_id not in present_stable_ids:
            outcomes[deck_id] = {"outcome": "missing", "stable_id": stable_id}
            continue
        was_playing = bool(deck.get("playing")) if deck else False
        if mode == "play" and was_playing:
            outcomes[deck_id] = {"outcome": "resumed", "stable_id": stable_id}
        else:
            outcomes[deck_id] = {"outcome": "paused", "stable_id": stable_id}
    return outcomes
