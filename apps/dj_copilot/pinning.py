"""SET-04 track pinning: peak-window math and reservation planning."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from apps.shared.harmonic import TrackFeature

from .set_goal import SetGoal

PEAK_WINDOW_FRAC = 0.15


def _avg_track_minutes() -> float:
    from .solver import AVG_TRACK_MINUTES

    return AVG_TRACK_MINUTES

PinRole = Literal["opener", "peak", "closer"]


class PinUnsatisfiableError(Exception):
    """Raised when pinned roles cannot be honored in the proposed order."""

    def __init__(self, reason: str, *, missing: tuple[str, ...] = ()) -> None:
        self.reason = reason
        self.missing = missing
        if missing:
            super().__init__(f"{reason}: {', '.join(missing)}")
        else:
            super().__init__(reason)


@dataclass(frozen=True, slots=True)
class SlotReservation:
    slot: int
    occurrence: int
    stable_id: str
    role: PinRole


def _first_occurrence(tracks: list[TrackFeature], stable_id: str) -> int | None:
    for i, track in enumerate(tracks):
        if track.stable_id == stable_id:
            return i
    return None


def peak_window_slot_indices(goal: SetGoal, total: int) -> list[int]:
    peak_t = (
        float(goal.peak_at_min)
        if goal.peak_at_min is not None
        else float(goal.duration_min) / 2.0
    )
    avg = _avg_track_minutes()
    window_min = max(avg, PEAK_WINDOW_FRAC * float(goal.duration_min))
    return [i for i in range(total) if abs(i * avg - peak_t) <= window_min]


def assign_peak_pin_slots(goal: SetGoal, total: int) -> dict[int, str]:
    """Map peak-window slot indices to peak pin stable_ids."""
    if not goal.peak_pins:
        return {}
    window = peak_window_slot_indices(goal, total)
    if goal.opener_pins:
        window = [slot for slot in window if slot != 0]
    if goal.closer_pin is not None and total > 0:
        last = total - 1
        window = [slot for slot in window if slot != last]
    n = len(goal.peak_pins)
    if n > len(window):
        raise PinUnsatisfiableError("peak_window_too_small")
    skip = (len(window) - n) // 2
    chosen = window[skip : skip + n]
    return dict(zip(chosen, goal.peak_pins, strict=True))


def _best_opener_occurrence(tracks: list[TrackFeature], goal: SetGoal) -> int:
    allowed = {
        i for i, track in enumerate(tracks) if track.stable_id in goal.opener_pins
    }
    if not allowed:
        raise PinUnsatisfiableError(
            "pin_not_in_playlist", missing=tuple(goal.opener_pins)
        )

    target_energy = float(goal.floor_energy)
    open_key = goal.open_on_key

    def key(occurrence: tuple[int, TrackFeature]) -> tuple[float, int, str, int]:
        position, track = occurrence
        e_dist = (
            abs(float(track.energy) - target_energy)
            if track.energy is not None
            else 10.0
        )
        key_mismatch = 1
        if open_key is not None and track.key_camelot is not None:
            from apps.shared.harmonic import camelot_distance, key_to_camelot

            try:
                key_mismatch = int(
                    camelot_distance(
                        key_to_camelot(open_key), key_to_camelot(track.key_camelot)
                    )
                )
            except ValueError:
                key_mismatch = 12
        return (e_dist, key_mismatch, track.stable_id, position)

    candidates = [(i, tracks[i]) for i in allowed]
    return min(candidates, key=key)[0]


def plan_reservations(
    tracks: list[TrackFeature], goal: SetGoal
) -> dict[int, SlotReservation]:
    """Plan fixed slot assignments for pinned roles."""
    total = len(tracks)
    if total == 0:
        return {}

    all_pins = list(goal.peak_pins) + list(goal.opener_pins)
    if goal.closer_pin is not None:
        all_pins.append(goal.closer_pin)
    missing = tuple(
        sid
        for sid in all_pins
        if _first_occurrence(tracks, sid) is None
    )
    if missing:
        raise PinUnsatisfiableError("pin_not_in_playlist", missing=missing)

    distinct_slots = len(goal.peak_pins)
    if goal.opener_pins:
        distinct_slots += 1
    if goal.closer_pin is not None:
        distinct_slots += 1
    if distinct_slots > total:
        raise PinUnsatisfiableError("playlist_too_short")

    reservations: dict[int, SlotReservation] = {}

    if goal.opener_pins:
        opener_occ = _best_opener_occurrence(tracks, goal)
        reservations[0] = SlotReservation(
            slot=0,
            occurrence=opener_occ,
            stable_id=tracks[opener_occ].stable_id,
            role="opener",
        )

    if goal.closer_pin is not None:
        closer_occ = _first_occurrence(tracks, goal.closer_pin)
        assert closer_occ is not None
        last = total - 1
        reservations[last] = SlotReservation(
            slot=last,
            occurrence=closer_occ,
            stable_id=goal.closer_pin,
            role="closer",
        )

    for slot, stable_id in assign_peak_pin_slots(goal, total).items():
        occurrence = _first_occurrence(tracks, stable_id)
        assert occurrence is not None
        reservations[slot] = SlotReservation(
            slot=slot,
            occurrence=occurrence,
            stable_id=stable_id,
            role="peak",
        )

    seen_occurrence: dict[int, SlotReservation] = {}
    for slot in sorted(reservations):
        reservation = reservations[slot]
        prior = seen_occurrence.get(reservation.occurrence)
        if prior is not None and prior.slot != slot:
            if reservation.role == "closer" or prior.role == "closer":
                raise PinUnsatisfiableError("closer_unsatisfiable")
            if reservation.role == "opener" or prior.role == "opener":
                raise PinUnsatisfiableError("opener_unsatisfiable")
            raise PinUnsatisfiableError("peak_window_too_small")
        seen_occurrence[reservation.occurrence] = reservation

    if goal.opener_pins and 0 not in reservations:
        raise PinUnsatisfiableError("opener_unsatisfiable")

    return reservations


def start_occurrence_for_pinned(
    tracks: list[TrackFeature],
    goal: SetGoal,
    reservations: dict[int, SlotReservation],
) -> int:
    if 0 in reservations:
        return reservations[0].occurrence
    from .solver import _best_start_occurrence

    return _best_start_occurrence(tracks, goal)
