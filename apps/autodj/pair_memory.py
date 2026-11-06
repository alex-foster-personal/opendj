"""Transition memory: the saved transition for an ordered pair of tracks (MEMORY-01, ADR-0129).

One row per ordered pair in ``pair_transitions``, most recent wins. :func:`record_played` is the save-on-run call: the
executor makes it when a transition completes, so whatever ran is the pair's default next time. Every plan is parsed by
``plan.py`` before it is stored, and a plan is stored only under the pair it names.

Functions take an open connection and run inside the caller's write unit.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from apps.autodj.contract.models import PairNotFound, PairTransition
from apps.autodj.plan import PlanError, parse_plan
from apps.shared.state.migrations_v25 import PAIR_TRANSITION_SOURCES
from apps.shared.state.migrations_v28 import PAIR_VERDICTS

_COLUMNS = "out_track_id, in_track_id, plan_json, source, style, created_at, updated_at, times_played"


# ----- helpers -------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row(row: tuple[Any, ...]) -> PairTransition:
    return PairTransition(row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7])


def _canonical(plan: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    """The plan as ``plan.py`` reads it, and its stored text; a document that is not a plan raises :class:`PlanError`."""
    document = parse_plan(plan).to_json()
    return document, json.dumps(document, sort_keys=True, separators=(",", ":"))


def _checked_source(source: str) -> str:
    if source not in PAIR_TRANSITION_SOURCES:
        raise PlanError(f"source must be one of {list(PAIR_TRANSITION_SOURCES)}, got {source!r}")
    return source


def _write(
    conn: sqlite3.Connection, document: dict[str, Any], text: str, *, source: str, style: str, played: int
) -> None:
    now = _now()
    conn.execute(
        f"INSERT INTO pair_transitions ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (out_track_id, in_track_id) DO UPDATE SET plan_json = excluded.plan_json, "
        "source = excluded.source, style = excluded.style, updated_at = excluded.updated_at, "
        "times_played = excluded.times_played",
        (document["out"]["track"], document["in"]["track"], text, source, style, now, now, played),
    )
    # A new plan for the pair is a different transition: a verdict on the old one must not be read as one on it.
    conn.execute(
        "DELETE FROM pair_verdicts WHERE out_track_id = ? AND in_track_id = ?",
        (document["out"]["track"], document["in"]["track"]),
    )


# ----- the store -----------------------------------------------------------


def get_pair_transition(conn: sqlite3.Connection, out_track_id: str, in_track_id: str) -> PairTransition | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM pair_transitions WHERE out_track_id = ? AND in_track_id = ?",
        (out_track_id, in_track_id),
    ).fetchone()
    return None if row is None else _row(row)


def _saved(conn: sqlite3.Connection, out_track_id: str, in_track_id: str) -> PairTransition:
    saved = get_pair_transition(conn, out_track_id, in_track_id)
    if saved is None:
        raise PairNotFound(f"no transition saved for {out_track_id} into {in_track_id}")
    return saved


def save_pair_transition(
    conn: sqlite3.Connection, plan: Mapping[str, Any], *, source: str, style: str
) -> PairTransition:
    """Make ``plan`` the pair's default. A replaced plan's play count starts again: it counts THIS plan's runs."""
    document, text = _canonical(plan)
    _write(conn, document, text, source=_checked_source(source), style=style, played=0)
    return _saved(conn, document["out"]["track"], document["in"]["track"])


def clear_pair_transition(conn: sqlite3.Connection, out_track_id: str, in_track_id: str) -> bool:
    cursor = conn.execute(
        "DELETE FROM pair_transitions WHERE out_track_id = ? AND in_track_id = ?", (out_track_id, in_track_id)
    )
    return cursor.rowcount > 0


def record_played(
    conn: sqlite3.Connection,
    out_track_id: str,
    in_track_id: str,
    *,
    plan: Mapping[str, Any] | None = None,
    source: str | None = None,
    style: str | None = None,
) -> PairTransition:
    """Save-on-run: the transition between this pair just completed.

    With ``plan`` (and its ``source`` and ``style``), that plan becomes the pair's default, unless it is the plan already
    saved, which only counts another run and keeps its source. Without one, the saved plan ran again; source or style
    without a plan is refused. :class:`PairNotFound` when there is none.
    """
    if plan is None:
        if source is not None or style is not None:
            raise PlanError("source or style without a plan is not a replay")
        return _bumped(conn, _saved(conn, out_track_id, in_track_id))
    if source is None or style is None:
        raise PlanError("a played plan needs its source and style")
    document, text = _canonical(plan)
    if (document["out"]["track"], document["in"]["track"]) != (out_track_id, in_track_id):
        raise PlanError(f"the plan is for {document['out']['track']} into {document['in']['track']}, not this pair")
    saved = get_pair_transition(conn, out_track_id, in_track_id)
    if saved is not None and _canonical(saved.plan)[1] == text:
        return _bumped(conn, saved)
    _write(conn, document, text, source=_checked_source(source), style=style, played=1)
    return _saved(conn, out_track_id, in_track_id)


def _bumped(conn: sqlite3.Connection, saved: PairTransition) -> PairTransition:
    conn.execute(
        "UPDATE pair_transitions SET times_played = times_played + 1, updated_at = ? "
        "WHERE out_track_id = ? AND in_track_id = ?",
        (_now(), saved.out_track_id, saved.in_track_id),
    )
    return _saved(conn, saved.out_track_id, saved.in_track_id)


# ----- the decision record and the verdict (AUTOPLAY-8881, ADR-0139) -----
# Pure logging: nothing in the planner reads these.


def save_decision(conn: sqlite3.Connection, out_track_id: str, in_track_id: str, decision: Mapping[str, Any]) -> None:
    """Store the lookahead's decision for this pair, replacing the last one: most recent wins, as for plans."""
    conn.execute(
        "INSERT INTO pair_decisions (out_track_id, in_track_id, decision_json, decided_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (out_track_id, in_track_id) DO UPDATE SET decision_json = excluded.decision_json, "
        "decided_at = excluded.decided_at",
        (out_track_id, in_track_id, json.dumps(dict(decision), sort_keys=True), _now()),
    )


def last_decision(conn: sqlite3.Connection, pair: tuple[str, str] | None = None) -> dict[str, Any] | None:
    """The newest stored decision (or this pair's), with the pair's saved plan meta and verdict when it has run."""
    where, args = ("WHERE d.out_track_id = ? AND d.in_track_id = ?", pair) if pair is not None else ("", ())
    row = conn.execute(
        "SELECT d.out_track_id, d.in_track_id, d.decision_json, d.decided_at, t.style, t.times_played, v.verdict, "
        "v.verdict_at FROM pair_decisions d LEFT JOIN pair_transitions t "
        "ON t.out_track_id = d.out_track_id AND t.in_track_id = d.in_track_id LEFT JOIN pair_verdicts v "
        "ON v.out_track_id = d.out_track_id AND v.in_track_id = d.in_track_id "
        f"{where} ORDER BY d.decided_at DESC LIMIT 1",
        args,
    ).fetchone()
    if row is None:
        return None
    return {
        "out_track_id": row[0],
        "in_track_id": row[1],
        "decision": json.loads(row[2]),
        "decided_at": row[3],
        # A saved plan that never ran (times_played 0) has not been heard, so it is not "played".
        "played": None if row[4] is None or not row[5] else {"style": row[4], "times_played": row[5]},
        "verdict": row[6],
        "verdict_at": row[7],
    }


def set_verdict(conn: sqlite3.Connection, out_track_id: str, in_track_id: str, verdict: str) -> PairTransition:
    """The listener's one-tap verdict on the transition that ran between this pair: good, meh or bad.

    :class:`PairNotFound` when nothing has run between them, so a verdict is never stored on a pair never heard."""
    if verdict not in PAIR_VERDICTS:
        raise ValueError(f"verdict must be one of {list(PAIR_VERDICTS)}, got {verdict!r}")
    saved = _saved(conn, out_track_id, in_track_id)
    if saved.times_played < 1:
        raise PairNotFound(f"the saved transition for {out_track_id} into {in_track_id} has never played")
    conn.execute(
        "INSERT INTO pair_verdicts (out_track_id, in_track_id, verdict, verdict_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (out_track_id, in_track_id) DO UPDATE SET verdict = excluded.verdict, "
        "verdict_at = excluded.verdict_at",
        (saved.out_track_id, saved.in_track_id, verdict, _now()),
    )
    return saved


# ----- replaying on other decks --------------------------------------------


def on_decks(plan: Mapping[str, Any], out_deck: int, in_deck: int) -> dict[str, Any]:
    """The saved plan moved onto the decks the pair is loaded on now.

    A pair saved on decks 1 and 2 may next play on 2 and 1, or 3 and 4. Odd decks sit left of the crossfader and even
    decks right, so a move across it mirrors every crossfader value.
    """
    if out_deck == in_deck:
        raise PlanError("out and in name the same deck")
    parsed = parse_plan(plan)
    document = parsed.to_json()
    decks = {parsed.out.deck: out_deck, parsed.in_.deck: in_deck}
    mirrored = parsed.out.deck % 2 != out_deck % 2
    document["out"]["deck"], document["in"]["deck"] = out_deck, in_deck
    for event in document["events"]:
        event["deck"] = decks.get(event["deck"])
        if mirrored and event["deck"] is None:
            for kind in ("set", "ramp"):
                if kind in event:
                    event[kind] = {"crossfader": round(1.0 - event[kind]["crossfader"], 6)}
    return parse_plan(document).to_json()
