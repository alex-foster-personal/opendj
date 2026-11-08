"""Column docs for the transition memory tables (schema v25; decision record and verdict v28)."""

from __future__ import annotations

PAIR_TRANSITION_COLUMN_DOCS: dict[str, dict[str, str]] = {
    "pair_transitions": {
        "out_track_id": "stable_id of the track playing out. Part of the primary key.",
        "in_track_id": "stable_id of the track coming in. Part of the primary key.",
        "plan_json": "The TransitionPlan document, as apps/autodj/plan.py parses it.",
        "source": "'planner' (a proposed plan ran), 'captured' (mixed by hand) or 'edited' (written through the API).",
        "style": "The transition style name, or 'captured' for a hand mix that matches no catalog style.",
        "created_at": "UTC stamp of the first plan saved for this pair.",
        "updated_at": "UTC stamp of the last write to this row.",
        "times_played": "How many times this plan has run. A replaced plan starts again.",
    },
    "pair_verdicts": {
        "out_track_id": "stable_id of the track playing out. Part of the primary key.",
        "in_track_id": "stable_id of the track coming in. Part of the primary key.",
        "verdict": "The listener's one-tap verdict on the transition that ran: 'good', 'meh' or 'bad' (v28).",
        "verdict_at": "UTC stamp of the last verdict (v28).",
    },
    "pair_decisions": {
        "out_track_id": "stable_id of the master AutoPlay was leaving. Part of the primary key.",
        "in_track_id": "stable_id of the track it chose, or '' when it chose none. Part of the primary key.",
        "decision_json": "The lookahead Decision: shortlist, windows, rejections, every candidate's style and rule score.",
        "decided_at": "UTC stamp of the decision. The newest row is GET /dj/transitions/last.",
    },
}
