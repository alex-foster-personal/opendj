"""DJ labels for own sections, from drums and bass energy.

WHY. All-In-One's ``harmonix-all`` labels are pop vocabulary (verse, chorus,
inst, solo). A DJ reads a dance track as energy: where the drums and bass are
all in, where they are pulled out, and what leads into what. The vocabulary
follows Raveform (TISMIR 2026): ``intro``, ``buildup``, ``drop``,
``breakdown``, ``cooldown``, ``outro``, plus ``ambient`` for an intro or
outro with no drums.

THE MEASUREMENT. The runner (``allin1_runner.py``) writes RMS envelopes in dB
for the Demucs ``drums`` and ``bass`` stems and the full ``mix`` at a fixed
hop. Each lane is normalized to the track itself: its reference is the 95th
percentile of 1 second window power, so 0 dB means "as loud as this lane
gets in this track" and the rules never depend on mastering loudness. A
section's level is its mean power over its frames, relative to that
reference. Its ``score`` is the weaker of drums and bass, because a drop
needs both.

THE RULES, in order, all deterministic:

1. ``full``: a section whose score is at least ``FULL_DB``.
2. The track must show contrast: at least one full section AND at least one
   section whose score is at or below ``WITHDRAWN_DB``. A track without both
   (flat energy throughout, or never full) is left UNLABELED with reason
   ``no_energy_contrast``: a drop is only a drop against something quieter,
   so labeling every section of a flat track ``drop`` would be a guess.
   Only sections at least ``CONTRAST_MIN_S`` long count as withdrawn here:
   a sub-second lead-in, or a fade tail kept as its own section, is quiet on
   every track and must not supply the contrast. Measured on a flat CC
   techno track, Wed 30 Sep 2026: every full-length section scored between
   -6.1 and -1.1 dB, while the model's 6 s ``end`` fade scored -29.4 dB.
3. Every full section is a ``drop``.
4. Before the first drop: section 0 is ``intro``. The section right before
   the first drop is ``buildup`` when it ``rises`` (below). A later
   pre-drop section whose score sits ``CONTRAST_DB`` or more under an
   earlier pre-drop section is a ``breakdown`` (energy pulled out after the
   intro had it). Everything else there is ``intro``.
5. Between drops: the section right before a drop is ``buildup`` when it
   rises, every other non-drop section is ``breakdown``.
6. After the last drop: the final section is ``outro``, the rest ``cooldown``.
7. ``rises``: the second half of the section is at least ``RISE_DB`` louder
   than its first half in the mix or in audible drums, or the drums are in
   (score of drums at least ``FULL_DB``) while the bass is still out.
8. ``ambient``: an ``intro`` or ``outro`` whose drums are at or below
   ``NO_DRUMS_DB``.

WHAT IS NEVER GUESSED. No envelopes, a silent drums or bass lane, a section
with no envelope frames, or no contrast: the answer is ``status:
unlabeled`` with a named reason and no per-section labels at all. Every
labeled section carries the energies it was decided on, so a reviewer can
audit a label against the numbers.

Pure stdlib, like ``quantize.py``.

-Claude
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

RULES = "raveform-energy-v1"
DJ_LABELS = ("intro", "buildup", "drop", "breakdown", "cooldown", "outro")

REF_PERCENTILE = 0.95
REF_WINDOW_S = 1.0
FULL_DB = -6.0  # score at or above: drums and bass both in
WITHDRAWN_DB = -12.0  # score at or below: drums or bass clearly pulled out
CONTRAST_MIN_S = 8.0  # a withdrawn section shorter than this cannot supply the contrast
CONTRAST_DB = 6.0  # a pre-drop section this far under an earlier one is a breakdown
RISE_DB = 3.0  # second half over first half, for a buildup
NO_DRUMS_DB = -15.0  # drums at or below: an ambient intro or outro
SILENT_LANE_DB = -70.0  # a lane whose reference is below this has nothing to normalize
FLOOR_DB = -120.0


def _power(db: float) -> float:
    return 10.0 ** (db / 10.0)


def _db(power: float) -> float:
    return 10.0 * math.log10(max(power, _power(FLOOR_DB)))


def _reference_db(frames_db: Sequence[float], hop_s: float) -> float:
    """95th percentile of 1 s window mean power, in dB."""
    win = max(1, round(REF_WINDOW_S / hop_s))
    powers = [_power(v) for v in frames_db]
    windows = sorted(
        sum(powers[i : i + win]) / len(powers[i : i + win]) for i in range(0, len(powers), win)
    )
    return _db(windows[int(REF_PERCENTILE * (len(windows) - 1))])


def _mean_db(frames_db: Sequence[float]) -> float:
    return _db(sum(_power(v) for v in frames_db) / len(frames_db))


def _frame_span(start_s: float, end_s: float, hop_s: float, n: int) -> tuple[int, int]:
    return max(0, math.floor(start_s / hop_s)), min(n, math.ceil(end_s / hop_s))


def _section_energy(
    section: dict[str, Any], env: dict[str, Any], refs: dict[str, float]
) -> dict[str, float] | None:
    hop = float(env["hop_s"])
    n = min(len(env[lane]) for lane in refs)
    lo, hi = _frame_span(float(section["start_s"]), float(section["end_s"]), hop, n)
    if hi - lo < 2:
        return None
    mid = (lo + hi) // 2
    out: dict[str, float] = {}
    for lane, ref in refs.items():
        frames = env[lane]
        out[f"{lane}_db"] = round(_mean_db(frames[lo:hi]) - ref, 1)
        out[f"{lane}_rise_db"] = round(_mean_db(frames[mid:hi]) - _mean_db(frames[lo:mid]), 1)
        out[f"{lane}_late_db"] = round(_mean_db(frames[mid:hi]) - ref, 1)
    out["score_db"] = min(out["drums_db"], out["bass_db"])
    out["seconds"] = round(float(section["end_s"]) - float(section["start_s"]), 3)
    return out


def _rises(e: dict[str, float]) -> bool:
    if e["mix_rise_db"] >= RISE_DB:
        return True
    if e["drums_rise_db"] >= RISE_DB and e["drums_late_db"] > NO_DRUMS_DB:
        return True
    return e["drums_db"] >= FULL_DB and e["bass_db"] < FULL_DB


def _pre_drop_label(i: int, first_drop: int, energies: list[dict[str, float]]) -> str:
    if i == 0:
        return "intro"
    if i == first_drop - 1 and _rises(energies[i]):
        return "buildup"
    earlier = max(e["score_db"] for e in energies[:i])
    return "breakdown" if energies[i]["score_db"] <= earlier - CONTRAST_DB else "intro"


def _label(i: int, drops: list[int], energies: list[dict[str, float]]) -> str:
    if i in drops:
        return "drop"
    if i < drops[0]:
        return _pre_drop_label(i, drops[0], energies)
    if i > drops[-1]:
        return "outro" if i == len(energies) - 1 else "cooldown"
    return "buildup" if (i + 1 in drops and _rises(energies[i])) else "breakdown"


def _unlabeled(reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": "unlabeled", "reason": reason, "rules": RULES, **extra}


def _references(env: dict[str, Any]) -> tuple[dict[str, float] | None, str | None]:
    hop = float(env.get("hop_s") or 0.0)
    lanes = ("drums", "bass", "mix")
    if hop <= 0 or any(not env.get(lane) for lane in lanes):
        return None, "malformed_envelopes"
    refs = {lane: _reference_db(env[lane], hop) for lane in lanes}
    silent = [lane for lane in lanes if refs[lane] < SILENT_LANE_DB]
    if silent:
        return None, f"silent_lane:{','.join(silent)}"
    return refs, None


def label_sections(
    sections: Sequence[dict[str, Any]], envelopes: dict[str, Any] | None
) -> dict[str, Any]:
    """DJ labels for ``sections`` (``start_s``/``end_s`` dicts) from runner envelopes.

    Returns ``{"status": "ok", "sections": [...], ...}`` with one entry per
    input section (``dj_label``, ``ambient``, ``energy``), or
    ``{"status": "unlabeled", "reason": ...}`` and no labels at all.
    """
    if not envelopes:
        return _unlabeled("no_envelopes")
    if not sections:
        return _unlabeled("no_sections")
    refs, why = _references(envelopes)
    if refs is None:
        return _unlabeled(why or "malformed_envelopes")
    energies = [_section_energy(s, envelopes, refs) for s in sections]
    known = [e for e in energies if e is not None]
    if len(known) != len(energies):
        # Name the real cause: a section reaching past the envelope's end,
        # or one simply too short (under 2 frames) to measure.
        env_end_s = float(envelopes["hop_s"]) * min(len(envelopes[lane]) for lane in refs)
        unmeasured = [s for s, e in zip(sections, energies, strict=True) if e is None]
        if any(float(s["end_s"]) > env_end_s for s in unmeasured):
            return _unlabeled("section_outside_envelope")
        return _unlabeled("section_too_short")
    return _apply_rules(known, refs)


def _apply_rules(energies: list[dict[str, float]], refs: dict[str, float]) -> dict[str, Any]:
    drops = [i for i, e in enumerate(energies) if e["score_db"] >= FULL_DB]
    withdrawn = [
        e for e in energies if e["score_db"] <= WITHDRAWN_DB and e["seconds"] >= CONTRAST_MIN_S
    ]
    if not drops or not withdrawn:
        return _unlabeled("no_energy_contrast", reference_db=_rounded(refs))
    out = []
    for i, e in enumerate(energies):
        label = _label(i, drops, energies)
        ambient = label in ("intro", "outro") and e["drums_db"] <= NO_DRUMS_DB
        out.append({"dj_label": label, "ambient": ambient, "energy": e})
    return {
        "status": "ok",
        "reason": None,
        "rules": RULES,
        "reference_db": _rounded(refs),
        "sections": out,
    }


def _rounded(refs: dict[str, float]) -> dict[str, float]:
    return {k: round(v, 1) for k, v in refs.items()}


__all__ = ["DJ_LABELS", "RULES", "label_sections"]
