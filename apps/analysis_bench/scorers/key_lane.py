"""Key lane scorer: MIREX weighted score, KSEA, mode accuracy, three-bucket
stratification against rekordbox and Mixed In Key.

WHY THIS SCORER OWNS ITS OWN RULER, UNLIKE beatgrid_lane.py. `beatgrid_lane.py`
adapts an EXISTING versioned scorer (`beatgrid.py`, pinned since round 0) behind
the shared interface. No key scorer exists yet anywhere in the repository, so
this module IS the ruler, in the same position `beatgrid.py`/`continuity.py`
hold for their lane: pure stdlib plus numpy, importable by pytest without a
heavy dependency.

MIREX WEIGHTED SCORE (1.0 / 0.5 / 0.3 / 0.2 / 0.0), ALLOWING DESCENDING FIFTHS.
Issue #1584 and the round-0 brief (`specs/native-analysis-v1-lanes/
nav1-key-r0.md`) both ask for "MIREX weighted (descending fifths allowed)".
`weighted_score` below credits 0.5 for EITHER direction of the fifth
(`(estimated_pc - reference_pc) % 12` in `{5, 7}`, same mode), which is the
literal, doubly-stated instruction, implemented here rather than importing
`mir_eval` per the issue's own preference.

DOCUMENTED DIVERGENCE FROM THE SHIPPED `mir_eval` LIBRARY. The issue's
parenthetical cites `mir_eval.key.weighted_score(..., allow_descending_fifths=
True)` as "the reference behavior". Checked live against the real, installed
`mir_eval` 0.8.2 (`uv run --with mir_eval`, Wed 9 Sep 2026): that function's
signature is `weighted_score(reference_key, estimated_key)` with NO
`allow_descending_fifths` parameter at all, and its shipped algorithm credits
0.5 ONLY for the ascending fifth (`% 12 == 7`); a descending fifth
(`% 12 == 5`) scores 0.0 there. This module deliberately implements the
BEHAVIOR the issue and the round-0 brief both specify in prose (descending
fifths credited), not the parenthetical's library citation, which does not
match the code it names. `scripts/keybench/verify_mirex.py` cross-checks
against real mir_eval as a control and documents this one expected
divergence rather than treating it as a bug.

KSEA -- THIS MODULE'S OWN DEFINITION, STATED HERE BECAUSE
`specs/native-analysis-v1.md` DOES NOT DEFINE IT. "Key Signature Estimation
Accuracy": the fraction of scored fixtures where the candidate's KEY
SIGNATURE matches the reference's, where "key signature" means the musical
equivalence class shared by a major key and its relative minor (e.g. C major
and A minor both have no sharps or flats). Formally, two keys share a
signature when `major_pitch_class(candidate) == major_pitch_class(reference)`,
where `major_pitch_class(k) = k.pitch_class if not k.is_minor else
(k.pitch_class + 3) % 12`. KSEA is distinct from MODE ACCURACY (mode match,
ignoring tonic) and from the exact MIREX 1.0 rate (both tonic AND mode): it
answers "did we find the right key signature", which is also exactly the
condition MIREX's own 0.3 "relative" credit is testing for.

THREE-BUCKET STRATIFICATION (spec section 5, "Agreement metric"). Every
fixture whose bundle carries BOTH a rekordbox and a MIK reference is
classified once, per arm:
  - AGREE: the two references canonicalize to the same (pitch_class,
    is_minor). The candidate's MIREX score is reported against that
    agreed value -- this is the only bucket with an unambiguous "correct".
  - DISAGREE-RELATED: the references disagree, and the candidate's answer
    canonicalizes to exactly one of them. Reported as "sides with rekordbox"
    or "sides with MIK", NEVER as "correct" -- neither reference is truth
    (`specs/native-analysis-v1-lanes/nav1-key-r0.md`: rekordbox scored 79.55,
    MIK 74.60, against GiantSteps, so both are themselves imperfect).
  - DISAGREE-UNRELATED: the references disagree and the candidate's answer
    matches neither. Enumerated by stable_id (spec: "enumerate, listen, prime
    key-change candidates") rather than just counted.
A fixture missing either reference cannot be classified into any of the three
buckets (there is no "which reference" to side with); it is counted and named
separately (`n_no_reference_pair`) rather than silently folded into the
denominator of a bucket it does not belong to.

HONEST DENOMINATORS, NEITHER REFERENCE IS TRUTH. Per the round-0 finding
above, a MIREX/KSEA/mode-accuracy figure computed "against rekordbox" and one
computed "against MIK" are reported SEPARATELY, each naming its own
denominator (the count of scored fixtures carrying THAT reference), rather
than blended into one number that hides which side it agrees with more.

OMITTED ANSWERS ARE SCORED AS WRONG, NOT DROPPED. Following the beatgrid
lane's `_OMITTED` precedent (Codex P1 BLOCKING, PR #1582): a fixture the
candidate did not answer is walked from the BUNDLE's fixture list, not the
candidate's results, and is scored as `Key | None = None` -- the worst
possible MIREX/KSEA/mode-accuracy outcome -- so a candidate cannot improve its
denominator by skipping its hard tracks.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from apps.analysis_key import canon

__all__ = ["SCORER_VERSION", "render_table", "score_bundle"]

SCORER_VERSION = "1.0.0"

KSEA_DEFINITION = (
    "KSEA (Key Signature Estimation Accuracy, this module's own definition -- "
    "not in specs/native-analysis-v1.md): the fraction of scored fixtures "
    "where the candidate and the reference share a key signature, i.e. "
    "major_pitch_class(candidate) == major_pitch_class(reference), where "
    "major_pitch_class(k) = k.pitch_class if major else (k.pitch_class + 3) % 12 "
    "(a key and its relative share a signature)."
)

# Controls first, same convention as beatgrid_lane.py: a reader should meet
# the floor and the ceiling before the candidate row whose numbers they read.
_ROLE_ORDER = {"negative_control": 0, "positive_control": 1, "positive_mik_control": 2}

_REFERENCES = ("rekordbox", "mik")


#-----------------------------------------------------------------------------
def _major_pitch_class(key: canon.Key) -> int:
    return (key.pitch_class + 3) % 12 if key.is_minor else key.pitch_class


def weighted_score(reference: canon.Key | None, estimated: canon.Key | None) -> float:
    """MIREX weighted score, 1.0 / 0.5 / 0.3 / 0.2 / 0.0, descending fifths allowed.

    See the module docstring for the documented divergence from `mir_eval`
    0.8.2's shipped algorithm (ascending fifth only).
    """
    if reference is None or estimated is None:
        return 0.0
    if reference == estimated:
        return 1.0
    delta = (estimated.pitch_class - reference.pitch_class) % 12
    if estimated.is_minor == reference.is_minor and delta in (5, 7):
        return 0.5
    if estimated.is_minor != reference.is_minor:
        if not reference.is_minor and delta == 9:
            return 0.3
        if reference.is_minor and delta == 3:
            return 0.3
        if delta == 0:
            return 0.2
    return 0.0


def _ksea_match(reference: canon.Key | None, estimated: canon.Key | None) -> bool:
    if reference is None or estimated is None:
        return False
    return _major_pitch_class(reference) == _major_pitch_class(estimated)


def _mode_match(reference: canon.Key | None, estimated: canon.Key | None) -> bool:
    if reference is None or estimated is None:
        return False
    return reference.is_minor == estimated.is_minor


#-----------------------------------------------------------------------------
def _parse_reference(value: str | None, source: str) -> canon.Key | None:
    """A truth-file value for one reference, or None for a documented absence."""
    if not value:
        return None
    if source == "rekordbox":
        return canon.from_rekordbox_scale_name(value)
    if source == "mik":
        return canon.from_mik_camelot(value)
    raise ValueError(f"unknown reference source {source!r}")  # pragma: no cover -- internal


def _parse_candidate(result: dict[str, Any] | None) -> canon.Key | None:
    """A candidate's answer for one fixture, accepting either notation it may
    emit (Camelot or Open Key -- both are real MIK/rekordbox output shapes),
    or None for an explicit error or an omitted answer.

    `key_openkey` (no underscore) is the field name used throughout the repo
    already -- `AnalysisRecord.key_openkey`, `apps/analysis/store.py`,
    `apps/analysis/backends/mik.py` -- so a candidate emitting the standard
    producer shape is accepted here rather than silently scored as a miss
    (Codex P1 BLOCKING, PR #1620).
    """
    if not result or result.get("error"):
        return None
    camelot = result.get("key_camelot")
    if camelot:
        return canon.from_mik_camelot(camelot)
    open_key = result.get("key_openkey")
    if open_key:
        return canon.from_mik_open_key(open_key)
    return None


_ReferenceMap = dict[str, canon.Key | None]


def load_bundle(bundle: Path) -> tuple[dict[str, Any], _ReferenceMap, _ReferenceMap]:
    """`(manifest, rekordbox references by stable_id, MIK references by stable_id)`.

    Public (unlike `beatgrid_lane.py`'s private `_cells`) because `controls.py`
    needs the same rekordbox reference the `truth_echo` control answers with,
    and re-parsing the truth file a second way there would risk drifting from
    what the scorer itself reads.
    """
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    reads = manifest.get("reads") or {}
    truth_name = reads.get("truth")
    if not truth_name:
        raise ValueError(f"{bundle}/manifest.json declares no reads.truth for the key lane")
    truth = json.loads((bundle / truth_name).read_text(encoding="utf-8"))["keys"]
    stable_ids = [row["stable_id"] for row in manifest.get("fixtures") or []]
    rekordbox = {sid: _parse_reference(truth.get(sid, {}).get("rekordbox"), "rekordbox")
                 for sid in stable_ids}
    mik = {sid: _parse_reference(truth.get(sid, {}).get("mik_camelot"), "mik")
           for sid in stable_ids}
    return manifest, rekordbox, mik


#-----------------------------------------------------------------------------
def _score_against(stable_ids: list[str], reference: _ReferenceMap,
                    answers: _ReferenceMap) -> dict[str, Any]:
    """MIREX/KSEA/mode-accuracy against ONE reference, named denominator = n."""
    scored = [sid for sid in stable_ids if reference[sid] is not None]
    if not scored:
        return {"n": 0, "mirex_mean_pct": None, "ksea_pct": None, "mode_accuracy_pct": None}
    mirex = [weighted_score(reference[sid], answers[sid]) for sid in scored]
    ksea = [_ksea_match(reference[sid], answers[sid]) for sid in scored]
    mode = [_mode_match(reference[sid], answers[sid]) for sid in scored]
    n = len(scored)
    return {
        "n": n,
        "mirex_mean_pct": round(100.0 * sum(mirex) / n, 2),
        "ksea_pct": round(100.0 * sum(ksea) / n, 2),
        "mode_accuracy_pct": round(100.0 * sum(mode) / n, 2),
    }


def _buckets(stable_ids: list[str], rekordbox: _ReferenceMap,
             mik: _ReferenceMap, answers: _ReferenceMap) -> dict[str, Any]:
    agree_ids, related_rb, related_mik, unrelated_ids, no_reference_pair = [], [], [], [], []
    agree_scores: list[float] = []
    for sid in stable_ids:
        rb_key, mik_key = rekordbox[sid], mik[sid]
        if rb_key is None or mik_key is None:
            no_reference_pair.append(sid)
            continue
        answer = answers[sid]
        if rb_key == mik_key:
            agree_ids.append(sid)
            agree_scores.append(weighted_score(rb_key, answer))
        elif answer == rb_key:
            related_rb.append(sid)
        elif answer == mik_key:
            related_mik.append(sid)
        else:
            unrelated_ids.append(sid)
    return {
        "agree": {
            "n": len(agree_ids),
            "mirex_mean_pct": round(100.0 * sum(agree_scores) / len(agree_scores), 2)
            if agree_scores else None,
        },
        "disagree_related": {
            "n": len(related_rb) + len(related_mik),
            "sides_with_rekordbox": len(related_rb),
            "sides_with_mik": len(related_mik),
        },
        "disagree_unrelated": {"n": len(unrelated_ids), "stable_ids": sorted(unrelated_ids)},
        "n_no_reference_pair": len(no_reference_pair),
    }


#-----------------------------------------------------------------------------
def score_bundle(bundle: Path, arms: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Score every arm of a round against one bundle's rekordbox+MIK truth."""
    manifest, rekordbox, mik = load_bundle(Path(bundle))
    stable_ids = list(rekordbox)
    scored: dict[str, Any] = {}
    for name, arm in arms.items():
        results = (arm["payload"].get("results")) or {}
        unknown = sorted(set(results) - set(stable_ids))
        if unknown:
            raise ValueError(
                f"this arm answered {len(unknown)} fixture(s) the bundle does not contain, "
                f"first {unknown[0]!r}. It was run against a different fixture set; "
                "scoring it here would attribute one bundle's numbers to another."
            )
        n_omitted = sum(1 for sid in stable_ids if sid not in results)
        answers = {sid: _parse_candidate(results.get(sid)) for sid in stable_ids}
        scored[name] = {
            "role": arm["role"],
            "note": arm.get("note", ""),
            "n_omitted": n_omitted,
            "vs_rekordbox": _score_against(stable_ids, rekordbox, answers),
            "vs_mik": _score_against(stable_ids, mik, answers),
            "buckets": _buckets(stable_ids, rekordbox, mik, answers),
        }
    return {
        "lane": "key",
        "scorer_version": SCORER_VERSION,
        "n_fixtures": len(stable_ids),
        "truth": (manifest.get("reads") or {}).get("truth", "inline"),
        "ksea_definition": KSEA_DEFINITION,
        "arms": scored,
    }


def render_table(report: dict[str, Any]) -> str:
    """The round's markdown: the KSEA definition, then one row per arm."""
    order = sorted(
        report["arms"].items(),
        key=lambda item: (_ROLE_ORDER.get(item[1]["role"], 2), item[0]),
    )
    lines = [
        report["ksea_definition"],
        "",
        f"n_fixtures (bundle denominator) = {report['n_fixtures']}",
        "",
        "| arm | role | n_omitted | vs rekordbox (n) | MIREX% | KSEA% | mode% | "
        "vs MIK (n) | MIREX% | KSEA% | mode% | AGREE (n, MIREX%) | "
        "DISAGREE-RELATED (rb/mik) | DISAGREE-UNRELATED (n: stable_ids) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, arm in order:
        rb, mikr, buckets = arm["vs_rekordbox"], arm["vs_mik"], arm["buckets"]
        agree, related, unrelated = (
            buckets["agree"], buckets["disagree_related"], buckets["disagree_unrelated"]
        )
        # The enumerated ids, not just the count, because rounds.append_round
        # persists only this rendered table -- the raw JSON's stable_ids list
        # is never written to the spec log (Codex P2 BLOCKING, PR #1620), and
        # spec section 5 asks to "enumerate, listen, prime key-change
        # candidates" for exactly this bucket.
        unrelated_ids = ", ".join(unrelated["stable_ids"]) or "-"
        lines.append(
            f"| {name} | {arm['role']} | {arm['n_omitted']} | "
            f"{rb['n']} | {rb['mirex_mean_pct']} | {rb['ksea_pct']} | {rb['mode_accuracy_pct']} | "
            f"{mikr['n']} | {mikr['mirex_mean_pct']} | {mikr['ksea_pct']} | "
            f"{mikr['mode_accuracy_pct']} | "
            f"{agree['n']}, {agree['mirex_mean_pct']} | "
            f"{related['sides_with_rekordbox']}/{related['sides_with_mik']} | "
            f"{unrelated['n']}: {unrelated_ids} |"
        )
    return "\n".join(lines)
