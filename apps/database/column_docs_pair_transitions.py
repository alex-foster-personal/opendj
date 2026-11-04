"""Column docs for the transition memory table (schema v25)."""

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
}
