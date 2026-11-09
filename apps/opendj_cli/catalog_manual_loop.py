"""Manual LOOP IN / OUT / RELOOP verbs registered after the main catalog table.

Kept out of ``catalog.py`` so small verb additions do not pull the whole opendj
import graph into pre-PR affected pytest (issue #9965, DECKUX-10478).
"""

from __future__ import annotations

from apps.opendj_cli import catalog
from apps.opendj_cli.catalog import PRESENT, Observe, Verb
from apps.opendj_cli.kinds import arg, deck_value

_DECK = arg("deck", "deck", deck_value, "1-4")

_MANUAL_LOOP_VERBS: tuple[Verb, ...] = (
    Verb(
        "loop_in",
        "loop_in",
        (_DECK,),
        observes=(Observe(("decks", "{deck}", "pending_loop_in_ms"), PRESENT),),
        note="DECKUX-10478: mark (or move) the manual loop-in point at the playhead, Q-snapped when Quantize is on.",
    ),
    Verb(
        "loop_out",
        "loop_out",
        (_DECK,),
        note="DECKUX-10478: engage pending loop in..playhead; refused (with the reason) without a pending in, or when out is not after in.",
    ),
    Verb(
        "reloop",
        "reloop",
        (_DECK,),
        note="DECKUX-10478: exit an engaged loop, or re-engage this track's last loop and jump to its in point; refused when there is none.",
    ),
)


def apply_manual_loop_verbs() -> None:
    for verb in _MANUAL_LOOP_VERBS:
        catalog.VERBS[verb.name] = verb
    catalog.VERB_NAMES = catalog.VERB_NAMES + tuple(verb.name for verb in _MANUAL_LOOP_VERBS)
