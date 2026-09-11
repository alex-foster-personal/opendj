"""Verdict-document emission: JSON document + markdown report, split out of
:mod:`apps.equivalence.verdict` (the file-size review gate). Purely
mechanical: :mod:`apps.equivalence.verdict` re-exports every name here, so
``from apps.equivalence.verdict import build_document`` (the existing import
shape) is unaffected.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apps.equivalence import SUITE_VERSION
from apps.equivalence.config import CFG
from apps.shared.source_fingerprint import fingerprint as _fingerprint

from .verdict_types import SUITE_NO_DATA, PairVerdict


def build_document(
    verdicts: list[PairVerdict],
    *,
    match_report: dict[str, Any],
    known_answers: dict[str, Any] | None,
    single_source: list[Any] | None = None,
    checked_at: str | None = None,
) -> dict[str, Any]:
    """The full verdict document, in ``apps.shared.equivalence`` shape.

    ``single_source`` carries :class:`~apps.equivalence.single_source.
    SingleSourceVerdict` entries for fields only one source has. They land in
    the same ``fields`` map with the same three contract statuses, but each
    declares ``basis: "single_source"`` and a ``suite_status`` of
    ``passed_single_source``, because a one-sided check is weaker evidence than
    two-source agreement and must never read as cross-validated.
    """
    stamp = checked_at or datetime.now(UTC).isoformat()
    fields = {v.field_name: v.entry(stamp) for v in verdicts}
    for verdict in single_source or []:
        fields[verdict.field_name] = verdict.entry(stamp)
    return {
        "meta": {
            "schema": "equivalence-verdicts/v1",
            "suite": "apps.equivalence",
            "suite_version": SUITE_VERSION,
            "generated_at": stamp,
            "consumer": "apps.shared.equivalence.EquivalenceGate",
            "rule": (
                "A mapping without a PASSING equivalence test is UNVERIFIED "
                "and must not feed the precedence policy."
            ),
            "basis_values": {
                "cross_source": (
                    "two sources hold the field; the verdict rests on "
                    "post-normalisation agreement"
                ),
                "single_source": (
                    "only one source holds the field, so NO agreement rate "
                    "exists and none was computed. Evidence is the full-column "
                    "probe, normaliser totality and (for a time series) the "
                    "structural invariants. WEAKER than cross_source"
                ),
            },
            # Each entry is a content fingerprint (path + size + mtime), not
            # a bare path string (P1 regression, PR #383 review): a consumer
            # binding its own live source to this verdict needs to tell
            # "same path, same file" from "same path, upgraded/replaced
            # file" -- path equality alone cannot, and a verdict computed
            # against the OLD file must not authorize values read from a
            # different one that merely happens to share its path.
            "sources": {
                "rekordbox": _fingerprint(CFG.rekordbox_db),
                "mik": _fingerprint(CFG.mik_db),
            },
            "matching": match_report,
            "known_answers": known_answers,
            "thresholds": {
                "min_comparable": CFG.min_comparable,
                "cluster_min_count": CFG.cluster_min_count,
                "cluster_min_share": CFG.cluster_min_share,
                "ratio_tolerance": CFG.ratio_tolerance,
            },
        },
        "fields": fields,
    }


def write_document(document: dict[str, Any], path: Path) -> Path:
    """Atomic write (tmp + rename) so a crash never leaves a torn gate file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(document, indent=2, sort_keys=False), encoding="utf-8")
    tmp.replace(path)
    return path


def _render_verdicts_table(document: dict[str, Any]) -> list[str]:
    """The summary table: one row per field."""
    lines: list[str] = [
        "## Verdicts",
        "",
        (
            "| Field | Verdict | Basis | Suite detail | Agreement "
            "(post-normalisation) | Comparable | Why |"
        ),
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, entry in document["fields"].items():
        agreement = entry.get("agreement")
        rate = "n/a"
        comparable = "0"
        if agreement and agreement.get("rate") is not None:
            rate = f"{agreement['rate']:.1%}"
            comparable = str(agreement["comparable"])
            if entry["suite_status"] == SUITE_NO_DATA:
                # A rate computed against an absent, constant or
                # undecidable-unit column is an ARTEFACT, and the per-field
                # section already says so. Measured Tue 28 Jul 2026: rating
                # rendered a bare '96.9%' in this table because rekordbox
                # 0-5 matched MIK's all-zero column on every unrated track.
                # A bare percentage in a summary row is what gets quoted, so
                # it must not appear without the word ARTEFACT next to it.
                rate = f"{rate} ARTEFACT, see below"
        elif entry.get("basis") == "single_source":
            rate = "none possible"
            comparable = str(entry["right"]["present"])
        why = entry["reasons"][0] if entry["reasons"] else ""
        lines.append(
            f"| `{name}` | **{entry['status'].upper()}** | "
            f"`{entry.get('basis', 'cross_source')}` | "
            f"`{entry['suite_status']}` | {rate} | {comparable} | "
            f"{why.replace('|', '/')} |"
        )
    lines += [
        "",
        (
            "`basis` matters: `cross_source` rests on agreement between two "
            "independent sources; `single_source` rests only on a full-column "
            "probe, normaliser totality and structural invariants, because "
            "nothing else holds the field. A single-source pass is WEAKER "
            "evidence and must not be read as cross-validated."
        ),
    ]
    return lines


def _render_matching_section(meta: dict[str, Any]) -> list[str]:
    lines: list[str] = ["", "## Matching", ""]
    matching = meta["matching"]
    lines.append(
        f"MIK rows {matching['mik_rows']}, rekordbox rows "
        f"{matching['rekordbox_rows']}, matched MIK rows "
        f"{matching['matched_mik_rows']} onto {matching['distinct_rekordbox_rows']} "
        f"distinct rekordbox rows, unmatched {matching['unmatched_mik_rows']}."
    )
    lines += ["", "| Tier | Matches | Confidence |", "| --- | --- | --- |"]
    for tier, count in matching["by_tier"].items():
        lines.append(
            f"| {tier} | {count} | {matching['tier_confidence'].get(tier, '')} |"
        )
    lines += ["", matching["note"], ""]
    return lines


def _render_field_probes(entry: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for side in ("left", "right"):
        probe = entry[side]
        if probe is None:
            continue
        lines.append(
            f"- **{probe['source']}** `{probe['locator']}` declared "
            f"`{probe['declared_unit']}`: {probe['present']} present, "
            f"{probe['null']} null, {probe['distinct']} distinct raw -> "
            f"{probe['canonical_distinct']} distinct canonical, raw "
            f"{probe['raw_min']!r}..{probe['raw_max']!r}, canonical "
            f"{probe['canonical_min']}..{probe['canonical_max']}, sentinels "
            f"{probe['sentinel_count']}, unmapped {probe['unmapped_count']}"
        )
        lines.extend(f"  - {finding}" for finding in probe["findings"])
    return lines


def _render_field_structure(structure: dict[str, Any]) -> list[str]:
    lines: list[str] = ["", "### Time-series structure", ""]
    lines.append(
        f"- {structure['segments']} spans over {structure['tracks']} "
        f"tracks ({structure['segments_per_track']['min']} to "
        f"{structure['segments_per_track']['max']} per track), spanning "
        f"{structure['min_start_s']:.6f} s to {structure['max_end_s']:.3f} s"
    )
    lines.append(
        f"- non-monotonic tracks {structure['non_monotonic_tracks']}, "
        f"overlapping tracks {structure['overlapping_tracks']}, "
        f"non-positive lengths {structure['non_positive_length_segments']}, "
        f"negative starts {structure['negative_start_segments']}, "
        f"tracks with gaps {structure['gap_tracks']}"
    )
    lines.append(
        f"- millisecond conversion: shortest span "
        f"{structure['min_length_s']:.4f} s, spans that would round away "
        f"to zero {structure['vanishing_segments_after_ms_rounding']}, "
        f"tracks overlapping after a NAIVE conversion "
        f"{structure['overlapping_tracks_naive_ms']}, after the "
        f"BOUNDARY-derived conversion "
        f"{structure['overlapping_tracks_boundary_ms']}"
    )
    lines += [f"- NOTE: {finding}" for finding in structure["findings"]]
    lines += [f"- VIOLATION: {violation}" for violation in structure["violations"]]
    return lines


def _render_field_consistency(consistency: dict[str, Any]) -> list[str]:
    lines: list[str] = ["", "### Scalar against its own time series", ""]
    lines.append(
        f"- {consistency['tracks_with_both']} tracks hold both; "
        f"{consistency['scalar_without_series']} hold the scalar with no "
        f"series, {consistency['series_without_scalar']} the reverse"
    )
    lines.append(
        f"- containment: scalar above the series max on "
        f"{consistency['scalar_above_series_max']} tracks, below the min "
        f"on {consistency['scalar_below_series_min']}"
    )
    lines.append(
        "- derivability, exact-match rate per hypothesis: "
        + ", ".join(
            f"`{name}` {count}"
            for name, count in consistency["hypothesis_exact_match"].items()
        )
    )
    lines += [f"- NOTE: {finding}" for finding in consistency["findings"]]
    lines += [f"- VIOLATION: {violation}" for violation in consistency["violations"]]
    return lines


def _render_field_bugs_and_clusters(entry: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if entry["mapping_bugs"]:
        lines += ["", "### MAPPING BUGS (proven: our error, not disagreement)", ""]
        for bug in entry["mapping_bugs"]:
            lines.append(
                f"- **{bug['kind']}**: "
                f"{bug.get('detail') or bug.get('interpretation', '')}"
            )
            lines += [f"  - {example}" for example in bug.get("examples", [])[:3]]
    suspects = [
        c for c in entry["systematic_offsets"] if c["classification"] == "suspect"
    ]
    if suspects:
        lines += ["", "### SUSPECT CLUSTERS (unresolved, need adjudication)", ""]
        for cluster in suspects:
            lines.append(
                f"- **{cluster['signature']}**: {cluster['count']} rows, "
                f"{cluster['share_of_disagreements']:.1%} of disagreements, "
                f"{cluster['share_of_comparable']:.1%} of the comparable "
                f"population. {cluster['interpretation']}"
            )
            lines += [f"  - {example}" for example in cluster.get("examples", [])[:3]]
    return lines


def _render_field_detail(name: str, entry: dict[str, Any]) -> list[str]:
    lines: list[str] = [f"## `{name}`", ""]
    if entry.get("basis") == "single_source":
        lines += [
            (
                f"Basis **single_source** ({entry.get('shape', 'scalar')}). "
                f"{entry.get('agreement_omitted_because', '')}"
            ),
            "",
        ]
    lines += _render_field_probes(entry)
    if entry.get("agreement") and entry["agreement"].get("form_counts"):
        lines.append(
            "- comparable rows by raw notation: "
            + ", ".join(
                f"`{form}` {count}"
                for form, count in entry["agreement"]["form_counts"].items()
            )
        )
    lines += [f"- {reason}" for reason in entry["reasons"]]
    structure = entry.get("structure")
    if structure:
        lines += _render_field_structure(structure)
    consistency = entry.get("series_consistency")
    if consistency:
        lines += _render_field_consistency(consistency)
    lines += _render_field_bugs_and_clusters(entry)
    if entry["disagreement_signatures"]:
        lines += ["", "Disagreement signatures (all of them, not a sample):", ""]
        lines += [
            f"- `{signature}`: {count}"
            for signature, count in entry["disagreement_signatures"].items()
        ]
    if entry["disagreement_csv"]:
        lines += ["", f"Disagreeing rows dumped to `{entry['disagreement_csv']}`."]
    lines.append("")
    return lines


def _render_known_answers(known: dict[str, Any]) -> list[str]:
    lines: list[str] = [
        "## Known-answer harness",
        "",
        (
            f"Fixture `{known['fixture']}`: {known['tracks']} tracks, "
            f"{known['passed']} pass, {known['failed']} fail, "
            f"{known['pending_human_verification']} pending human "
            f"verification, {known['unresolved_tracks']} unresolved "
            f"tracks, {known['unresolved_checks']} checks that never ran."
        ),
        "",
        known["note"],
        "",
        "| Track | Target | Expected | Actual | Verified by | Status |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    lines.extend(
        f"| {result['track_id']} | `{result['target']}` | "
        f"{result['expected_raw']!r} | {result['actual_raw']!r} | "
        f"{result['verified_by']} | {result['status']} |"
        for result in known["results"]
    )
    lines.append("")
    return lines


def render_report(document: dict[str, Any]) -> str:
    """Human-readable markdown. Mirrors the JSON; never adds a claim to it."""
    meta = document["meta"]
    lines: list[str] = [
        "# Equivalence suite report (rekordbox vs Mixed In Key)",
        "",
        (
            f"Generated {meta['generated_at']} by `{meta['suite']}` "
            f"v{meta['suite_version']}."
        ),
        "",
        "> " + meta["rule"],
        "",
        "Section 4b of `.agents/skills/novel-music-app-onboarding/SKILL.md` as",
        "runnable code. An agreement rate below a FAILED verdict is not a",
        "result: the two columns were not proven to be the same field.",
        "",
    ]
    lines += _render_verdicts_table(document)
    lines += _render_matching_section(meta)
    for name, entry in document["fields"].items():
        lines += _render_field_detail(name, entry)
    known = meta.get("known_answers")
    if known:
        lines += _render_known_answers(known)
    return "\n".join(lines)

