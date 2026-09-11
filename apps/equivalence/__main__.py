"""CLI for the equivalence suite. Agent-native: every flow is scriptable.

    python -m apps.equivalence probe          --data-dir data [--json]
    python -m apps.equivalence run            --data-dir data [--json] [--write]
    python -m apps.equivalence known-answers  --data-dir data [--json]

``run`` is the full suite: probe, normalise, agreement, offset clustering,
known answers, verdict. It writes ``<data-dir>/state/equivalence-verdicts.json``
and ``equivalence-report.md`` unless ``--no-write`` is passed.

Exit codes are load-bearing so this can gate a pipeline. They are distinct
because the right response to each differs: a mapping bug is a code fix, a
known-answer failure is a reader or fixture regression, and an unreadable
source is an environment problem.

| Code | Meaning |
|---|---|
| 0 | clean: no field FAILED and every known-answer check passed |
| 2 | at least one field FAILED, i.e. a proven mapping bug |
| 3 | a known-answer check failed or a fixture track did not resolve |
| 4 | a source could not be read |

An UNTESTED verdict is NOT an error exit: "we have not proven this yet" is a
legitimate steady state, and the gate in ``apps.shared.equivalence`` already
refuses to write those fields.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from apps.equivalence.compare import (
    compare_pair,
    detect_offset_clusters,
    dump_disagreements,
    signature_histogram,
)
from apps.equivalence.config import (
    CFG,
    FIELD_PAIRS,
    FIXTURE_PATH,
    SINGLE_SOURCE_FIELDS,
    FieldPair,
)
from apps.equivalence.known import run_known_answers
from apps.equivalence.probe import probe_field
from apps.equivalence.single_source import (
    audit_scalar_against_series,
    audit_series,
    decide_single_source,
    probe_single_source,
)
from apps.equivalence.sources import (
    Pairing,
    match,
    read_mik,
    read_mik_energy_segments,
    read_rekordbox,
)
from apps.equivalence.verdict import (
    FAILED,
    build_document,
    decide,
    render_report,
    write_document,
)

EXIT_OK = 0
EXIT_MAPPING_BUG = 2
EXIT_KNOWN_ANSWER_FAILED = 3
EXIT_SOURCE_UNREADABLE = 4


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m apps.equivalence",
        description=(
            "Prove two sources' fields are apples-to-apples before any "
            "agreement rate or precedence policy is believed."
        ),
    )
    parser.add_argument("command", choices=("probe", "run", "known-answers"))
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="data root holding master.plain.db and state/ (default: data)",
    )
    parser.add_argument(
        "--mik-db",
        type=Path,
        default=None,
        help=f"path to a .mikdb (default: {CFG.mik_db})",
    )
    parser.add_argument(
        "--fields",
        default=None,
        help="comma-separated field names to test (default: all declared)",
    )
    parser.add_argument(
        "--fixture", type=Path, default=FIXTURE_PATH, help="known-answer fixture"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="do not write the verdict file or the report",
    )
    parser.add_argument(
        "--no-fuzzy",
        action="store_true",
        help="exact-path matching only (drops the basename and artist+title tiers)",
    )
    return parser


def _selected(names: str | None):
    if not names:
        return FIELD_PAIRS
    wanted = {n.strip() for n in names.split(",") if n.strip()}
    chosen = tuple(p for p in FIELD_PAIRS if p.field_name in wanted)
    # A source-unique name is a legal selection even though it is not a PAIR.
    # ``energy`` moved from FIELD_PAIRS to SINGLE_SOURCE_FIELDS on
    # Tue 28 Jul 2026, and `--fields energy` must keep working across that move.
    single = {s.field_name for s in SINGLE_SOURCE_FIELDS}
    missing = wanted - {p.field_name for p in chosen} - single
    if missing:
        raise SystemExit(
            f"unknown field(s): {sorted(missing)}; declared fields are "
            f"{[p.field_name for p in FIELD_PAIRS] + sorted(single)}"
        )
    return chosen


def _run_probe_command(
    rb_rows: list[Any],
    mik_rows: list[Any],
    pairs: tuple[FieldPair, ...],
    args: argparse.Namespace,
) -> int:
    payload = {
        pair.field_name: {
            "left": probe_field(rb_rows, pair.left).as_dict(),
            "right": probe_field(mik_rows, pair.right).as_dict(),
        }
        for pair in pairs
    }
    _emit(payload, args.json, _render_probe(payload))
    return EXIT_OK


def _run_known_answers_command(
    mik_rows: list[Any], pairings: list[Pairing], args: argparse.Namespace
) -> int:
    report = run_known_answers(mik_rows, pairings, fixture_path=args.fixture).as_dict()
    _emit(report, args.json, _render_known(report))
    return EXIT_OK if report["ok"] else EXIT_KNOWN_ANSWER_FAILED


def _build_verdicts(
    pairs: tuple[FieldPair, ...],
    rb_rows: list[Any],
    mik_rows: list[Any],
    pairings: list[Pairing],
    args: argparse.Namespace,
) -> list[Any]:
    verdicts = []
    for pair in pairs:
        left = probe_field(rb_rows, pair.left)
        right = probe_field(mik_rows, pair.right)
        agreement, disagreements = compare_pair(pairings, pair)
        # Cluster ONLY when both sides can actually hold a value. A constant
        # column (MIK ZRATING is 0 on all 7026 rows) otherwise produces
        # 'additive_+5' clusters that read as a convention error when the real
        # finding is that one side has no data at all.
        if left.comparable and right.comparable:
            clusters = detect_offset_clusters(
                disagreements,
                kind=pair.left.kind,
                comparable=agreement.comparable,
                form_counts=agreement.form_counts,
            )
        else:
            clusters = []
        verdict = decide(pair, left, right, agreement, clusters)
        verdict.signature_histogram = signature_histogram(disagreements)
        if disagreements and not args.no_write:
            csv_path = dump_disagreements(
                disagreements,
                CFG.disagreement_dir / f"{pair.field_name}-disagreements.csv",
            )
            verdict.disagreement_csv = str(csv_path)
        verdicts.append(verdict)
    return verdicts


def _write_gate_if_trustworthy(
    document: dict[str, Any], *, trustworthy: bool, args: argparse.Namespace
) -> None:
    # The canonical gate feeds apps.shared.equivalence.EquivalenceGate on the
    # NEXT MIK load, so a run this suite itself does not trust must never
    # reach it: a mapping bug or a known-answer failure means the verdicts
    # above are unproven, not merely disappointing. ``--fields`` also narrows
    # ``document["fields"]`` to a subset -- writing that wholesale would
    # demote every unselected field's already-proven verdict back to
    # untested, so a partial run merges onto whatever gate already exists.
    if args.no_write:
        return
    if not trustworthy:
        print(
            "\nNOT writing the gate file: this run is not trustworthy "
            "(a FAILED field or a known-answer failure below). Pass "
            "--no-write to silence this note, or fix the run first.",
            file=sys.stderr,
        )
        return
    if args.fields:
        document = _merge_with_existing_gate(document, CFG.verdicts_path)
    write_document(document, CFG.verdicts_path)
    CFG.report_path.write_text(render_report(document), encoding="utf-8")
    document["meta"]["written_to"] = {
        "verdicts": str(CFG.verdicts_path),
        "report": str(CFG.report_path),
    }


def _report_and_exit(
    document: dict[str, Any],
    *,
    failed: list[str],
    known: dict[str, Any],
    args: argparse.Namespace,
) -> int:
    if args.json:
        print(json.dumps(document, indent=2))
    else:
        print(render_report(document))
    if failed:
        print(
            f"\nFAILED fields (mapping bugs, not source disagreement): {failed}",
            file=sys.stderr,
        )
        return EXIT_MAPPING_BUG
    if not known["ok"]:
        print(
            f"\nKNOWN-ANSWER failures: {known['failed']} failed, "
            f"{known['pending_human_verification']} pending human "
            f"verification, {known['unresolved_tracks']} unresolved tracks, "
            f"{known['unresolved_checks']} unresolved checks. Our reader or the "
            f"fixture has drifted, or a needs-maintainer entry is still unsettled; "
            f"the verdicts above are not trustworthy until that is settled.",
            file=sys.stderr,
        )
        return EXIT_KNOWN_ANSWER_FAILED
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    CFG.resolve(args.data_dir)
    if args.mik_db is not None:
        CFG.mik_db = args.mik_db
    pairs = _selected(args.fields)

    try:
        rb_rows = read_rekordbox(CFG.rekordbox_db)
        mik_rows = read_mik(CFG.mik_db)
    except (FileNotFoundError, sqlite3.DatabaseError, sqlite3.OperationalError) as exc:
        # A source that exists but is corrupt, locked, or has drifted schema
        # raises sqlite3.DatabaseError/OperationalError, not FileNotFoundError
        # (P2 review, PR #383): that is still an unreadable-source condition,
        # not an unspecified crash, so it gets the same advertised
        # EXIT_SOURCE_UNREADABLE rather than a bare traceback and exit code 1.
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_SOURCE_UNREADABLE

    if args.command == "probe":
        return _run_probe_command(rb_rows, mik_rows, pairs, args)

    pairings, match_report = match(rb_rows, mik_rows, allow_fuzzy=not args.no_fuzzy)

    if args.command == "known-answers":
        return _run_known_answers_command(mik_rows, pairings, args)

    known = run_known_answers(mik_rows, pairings, fixture_path=args.fixture).as_dict()
    verdicts = _build_verdicts(pairs, rb_rows, mik_rows, pairings, args)
    single = _run_single_source(mik_rows, only=args.fields)
    document = build_document(
        verdicts,
        match_report=match_report.as_dict(),
        known_answers=known,
        single_source=single,
    )
    failed = [v.field_name for v in verdicts + single if v.status == FAILED]
    trustworthy = not failed and known["ok"]

    _write_gate_if_trustworthy(document, trustworthy=trustworthy, args=args)
    return _report_and_exit(document, failed=failed, known=known, args=args)


def _merge_with_existing_gate(document: dict[str, Any], path: Path) -> dict[str, Any]:
    """Layer this run's verdicts onto any prior gate file's fields.

    ``--fields`` scopes ``document["fields"]`` to the selected subset; a
    wholesale write would erase every unselected field's proven verdict.
    Corrupt or absent prior state is not this function's problem to solve --
    it degenerates to "nothing to merge", not a mask of a real failure.
    """
    if not path.exists():
        return document
    try:
        prior = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return document
    document["fields"] = {**prior.get("fields", {}), **document["fields"]}
    return document


# ------------------------------------------------------- single source


def _run_single_source(mik_rows: list[Any], *, only: str | None) -> list[Any]:
    """Verdicts for fields only MIK has, so no agreement rate can exist."""
    wanted = {n.strip() for n in only.split(",") if n.strip()} if only else None
    out: list[Any] = []
    for spec in SINGLE_SOURCE_FIELDS:
        if wanted is not None and spec.field_name not in wanted:
            continue
        if spec.shape == "time_series":
            segments = read_mik_energy_segments(CFG.mik_db)
            # Probe the VALUES via the shared full-column probe by presenting
            # each segment as a row, then audit the SPANS separately: a series
            # has invariants a scalar does not.
            probe = probe_field(segments, spec.source)
            structure = audit_series(
                segments,
                value_range=spec.value_range or (1.0, 10.0),
            )
            out.append(decide_single_source(spec, probe, structure))
            continue
        # A scalar with a companion series in the same source gets the one
        # cross-check available without a second source: it must lie inside its
        # OWN series' range. Anything else means the two columns are different
        # quantities (SKILL 4b, the DJ.Studio energyLevelNr case).
        consistency = None
        if spec.companion_series_field is not None:
            consistency = audit_scalar_against_series(
                {row.pk: getattr(row, spec.source.attr, None) for row in mik_rows},
                read_mik_energy_segments(CFG.mik_db),
            )
        out.append(
            decide_single_source(
                spec,
                probe_single_source(mik_rows, spec),
                series_consistency=consistency,
            )
        )
    return out


# ------------------------------------------------------------- rendering


def _emit(payload: dict[str, Any], as_json: bool, text: str) -> None:
    print(json.dumps(payload, indent=2) if as_json else text)


def _render_probe(payload: dict[str, Any]) -> str:
    lines = ["Range and cardinality probe (full column, never a sample)", ""]
    for name, sides in payload.items():
        lines.append(f"{name}:")
        for side in ("left", "right"):
            probe = sides[side]
            lines.append(
                f"  {probe['source']:<10} {probe['locator']:<40} "
                f"unit={probe['declared_unit']:<22} present={probe['present']:<6} "
                f"distinct={probe['distinct']:<5} raw={probe['raw_min']!r}"
                f"..{probe['raw_max']!r}"
            )
            lines.extend(f"    ! {finding}" for finding in probe["findings"])
        lines.append("")
    return "\n".join(lines)


def _render_known(report: dict[str, Any]) -> str:
    lines = [
        (
            f"Known-answer harness: {report['tracks']} tracks, "
            f"{report['passed']} pass, {report['failed']} fail, "
            f"{report['pending_human_verification']} pending, "
            f"{report['unresolved_tracks']} unresolved tracks, "
            f"{report['unresolved_checks']} checks that never ran"
        ),
        "",
    ]
    for result in report["results"]:
        lines.append(
            f"  [{result['status']:<10}] {result['track_id']:<34} "
            f"{result['target']:<26} expected={result['expected_raw']!r} "
            f"actual={result['actual_raw']!r} ({result['verified_by']})"
        )
        if result["detail"]:
            lines.append(f"               {result['detail']}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
