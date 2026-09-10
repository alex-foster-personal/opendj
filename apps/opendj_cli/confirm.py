"""Confirm an order against the mirror, because a 200 is not an outcome.

``"status": "succeeded"`` means the open page CLAIMED the order and its typed
dispatcher did not throw. It does not mean the audio changed. Observed live on
Thu 10 Sep 2026: a ``play`` on deck 1 returned ``succeeded`` while the deck
stayed ``playing=false``, and deck 3 accepted nine transport commands with
``presented_revision`` stuck at 0. So after every order the CLI re-reads the
mirror and reports one of three verdicts:

``confirmed``
    Every check passed: the control the verb names agrees in the mirror, and
    the deck's presentation clock has nothing outstanding
    (``desired_revision == presented_revision``).
``unconfirmed``
    A check exists and did not pass inside the settle deadline. The CLI names
    what it expected and what the mirror says instead.
``accepted``
    Nothing in the mirror can verify this command (the AGENT-02 gap), so the
    CLI reports acceptance only and says so rather than implying more.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from apps.opendj_cli.catalog import Observe, Verb

SETTLE_TIMEOUT_S = 3.0
SETTLE_POLL_S = 0.05

MISSING = object()


@dataclass(frozen=True)
class Check:
    """One condition the mirror must satisfy before an order is confirmed."""

    description: str
    path: tuple[str, ...]
    expected: Any = MISSING
    tolerance: float | None = None
    require_settled: bool = False

    def unmet_because(self, mirror: Mapping[str, Any]) -> str | None:
        """Why the mirror does not yet satisfy this check, or None if it does."""
        where = ".".join(self.path)
        value = read_path(mirror, self.path)
        if value is MISSING:
            return f"{where} is absent from the mirror"
        if self.require_settled:
            return None if _settled(value) else _unsettled(where, value)
        return self._mismatch(where, value)

    def _mismatch(self, where: str, value: Any) -> str | None:
        if self.expected is MISSING:
            return None if value is not None else f"{where} is still null"
        if self.tolerance is None:
            if value == self.expected:
                return None
            return f"{where} is {value!r}, expected {self.expected!r}"
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return f"{where} is {value!r}, not a number"
        if abs(value - self.expected) > self.tolerance:
            return f"{where} is {value!r}, expected {self.expected!r}"
        return None


def _field(container: Any, key: str) -> Any:
    if isinstance(container, Mapping):
        return container.get(key, "absent")
    return "absent"


def _unsettled(where: str, presentation: Any) -> str:
    return (
        f"{where} has not been presented "
        f"(desired_revision={_field(presentation, 'desired_revision')}, "
        f"presented_revision={_field(presentation, 'presented_revision')})"
    )


def _settled(presentation: Any) -> bool:
    if not isinstance(presentation, Mapping):
        return False
    desired = presentation.get("desired_revision", MISSING)
    presented = presentation.get("presented_revision", MISSING)
    if desired is MISSING or presented is MISSING:
        return False
    return desired == presented


def read_path(document: Mapping[str, Any], path: Sequence[str]) -> Any:
    """Follow ``path`` through mappings, returning ``MISSING`` if it stops."""
    node: Any = document
    for segment in path:
        if not isinstance(node, Mapping) or segment not in node:
            return MISSING
        node = node[segment]
    return node


def _format_segment(segment: str, command: Mapping[str, Any]) -> str:
    try:
        return segment.format(**command)
    except KeyError as error:
        raise ValueError(
            f"mirror path segment {segment!r} needs command field {error.args[0]!r}"
        ) from None


def _observe_check(observe: Observe, command: Mapping[str, Any]) -> Check:
    path = tuple(_format_segment(segment, command) for segment in observe.path)
    expected = MISSING if observe.expect is None else command[observe.expect]
    return Check(
        description=f"{'.'.join(path)} == {expected!r}",
        path=path,
        expected=expected,
        tolerance=observe.tolerance,
    )


def checks_for(verb: Verb, command: Mapping[str, Any]) -> tuple[Check, ...]:
    """Every mirror condition one dispatched command must satisfy.

    A command that names a deck always carries the presentation-clock check,
    even when the verb declares no observation: that is the check the live
    ``succeeded``-but-nothing-happened fault would have failed.
    """
    checks = [_observe_check(observe, command) for observe in verb.observes]
    deck = command.get("deck")
    if deck is not None:
        checks.append(
            Check(
                description=f"deck {deck} presentation clock is settled",
                path=("decks", str(deck), "presentation_clock"),
                require_settled=True,
            )
        )
    return tuple(checks)


def verdict(checks: Sequence[Check], mirror: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    """``confirmed`` / ``unconfirmed`` / ``accepted`` plus the reasons it is not."""
    if not checks:
        return "accepted", ()
    failures = tuple(
        failure for check in checks if (failure := check.unmet_because(mirror)) is not None
    )
    if failures:
        return "unconfirmed", failures
    return "confirmed", ()
