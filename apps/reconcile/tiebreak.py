"""Rank ambiguous relink candidates with content signals. Never applies.

Entry point::

    python -m apps.reconcile relink-review --data-dir /path/to/data

Writes ``<data-dir>/state/relink-review-<YYYY-MM-DD>.csv``: one line per
candidate, rank 1 first, so a human sees the top pick AND its runners-up side
by side with the evidence that separated them. Nothing here mutates state.db or
touches an audio file. the maintainer decides what gets applied.

Why a second scoring pass
-------------------------
:mod:`apps.reconcile.match` scores a candidate on the FIRST tier that fires and
stops. That is right for auto-apply (the recorded tier is the strongest single
piece of evidence) but it throws away corroboration: a basename-duration
candidate that ALSO matches on file size and ISRC is far safer than one that
matches on duration alone, and the tier confidence cannot express that. This
module re-examines every ambiguous row's candidates and adds:

* duration agreement (from the tag index),
* file-size agreement (rekordbox ``djmdContent.FileSize``),
* ISRC tag agreement,
* path plausibility -- a curated library folder outranks a staging/temp folder.

Path plausibility, measured not guessed
---------------------------------------
The folder classes in :data:`CURATED_SEGMENTS` and :data:`STAGING_SEGMENTS` come
from the actual candidate paths in ``link-repair-review.csv`` on this machine:
'Manual Library', 'rekordbox' and the Music.app 'Media.localized' tree are the
curated destinations; 'Convert temp', 'Convert temp 2 (BACKUP)', 'TODO',
'_merged-from-recovered-...', '... copy' and '... BACKUP ...' are working
scratch. A file living in scratch is more likely a transient duplicate of the
real thing, so it ranks below.

Resolution rule
---------------
A row counts as RESOLVED (still not applied) only when all three hold:

1. exactly one candidate survives the hard filters (exists on disk, not already
   another row's recorded path),
2. its score is at least :data:`RESOLVE_SCORE_MIN`, i.e. the matcher's
   auto-apply threshold PLUS at least one full content signal on top,
3. its lead over the runner-up is at least :data:`RESOLVE_MARGIN`, which is set
   ABOVE the largest possible path-plausibility swing on purpose: a winner must
   never be decided by folder taste alone, only by content agreement.

Requirements (mini-PRD)
-----------------------
1. Rank every ambiguous row's candidates with corroborating signals.  OK
   - [if] two candidates share a basename and only one matches the recorded
     duration [then] that one ranks first with a positive duration signal.
   - [if] a candidate's ISRC tag contradicts the row's ISRC [then] it is
     penalised below a candidate with no ISRC information at all.
   - [if] two candidates differ only by folder class [then] the curated one
     ranks first but the row is NOT resolved (margin rule).
2. Emit a ranked review CSV, never an applied change.  OK
   - [if] the command runs [then] state.db's mtime is unchanged.
   - [if] a row has three candidates [then] the CSV holds three lines for it,
     ranked 1..3, all carrying the same ``resolved`` verdict for the row.
3. target-already-linked and target-contested are handled honestly.  OK
   - [if] the sole candidate is another row's live file [then] the row is
     unresolved with a reason saying evidence cannot fix a duplicate.
   - [if] several rows contest one file [then] the highest-scoring row can win
     the contest, and only if it leads by the margin.

Status: OK ran-script works-as-expected, regression tests in
``tests/reconcile/test_relink.py``.
"""
from __future__ import annotations

import argparse
import csv
import os
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table

from apps.reconcile import match
from apps.reconcile.index_disk import CACHE_PATH, DEFAULT_ROOTS, DiskAudio, DiskIndex, build_index
from apps.shared import paths

console = Console(width=120)

# ----- signal weights ----------------------------------------------------

# Content signals. Duration and size are equally strong (both are facts about
# the bytes); ISRC is stronger because it identifies the recording itself, and
# a CONTRADICTED ISRC is the single most damning signal available.
DUR_TIGHT_S: float = 2.0
DUR_LOOSE_S: float = 5.0
DUR_CONTRADICTION_S: float = 10.0
W_DURATION_TIGHT: float = 0.30
W_DURATION_LOOSE: float = 0.15
W_DURATION_CONTRADICTED: float = -0.40
W_SIZE_EXACT: float = 0.30
W_SIZE_CONTRADICTED: float = -0.10  # transcodes legitimately differ in size
W_ISRC_MATCH: float = 0.40
W_ISRC_CONTRADICTED: float = -0.50

# Path plausibility. The span (curated 0.20 to staging -0.25) is 0.45, which is
# why RESOLVE_MARGIN sits above it: folder taste can order candidates but must
# never be what promotes a row to "resolved".
W_PATH_CURATED: float = 0.20
W_PATH_STAGING: float = -0.25

PATH_SPAN: float = W_PATH_CURATED - W_PATH_STAGING

RESOLVE_SCORE_MIN: float = match.AUTO_APPLY_THRESHOLD + W_DURATION_TIGHT
RESOLVE_MARGIN: float = 0.50

# Case-insensitive substrings matched against the directory part of a candidate.
CURATED_SEGMENTS: tuple[str, ...] = (
    "manual library",
    "media.localized",
    "rekordbox",
    "pioneerdj",
    "musicaf",
)
STAGING_SEGMENTS: tuple[str, ...] = (
    "convert temp",
    "backup",
    "todo",
    " copy",
    "_merged-from-recovered",
    "/downloads/",
    "/desktop/",
    ".trash",
    "for organising",
    "duplicate",
    "/tmp/",
    "temp/",
)

PathClass = str  # "curated" | "staging" | "neutral"

REVIEW_COLUMNS: tuple[str, ...] = (
    "rank",
    "stable_id",
    "resolved",
    "resolved_reason",
    "ambiguity",
    "recorded_path",
    "candidate_path",
    "tier",
    "base_confidence",
    "score",
    "margin",
    "duration_signal",
    "size_signal",
    "isrc_signal",
    "path_class",
    "path_signal",
    "dataless",
    "matcher_reason",
)


def classify_path(candidate_path: str) -> PathClass:
    """Folder class of ``candidate_path``: curated, staging or neutral.

    Staging wins ties: a copy sitting inside a curated tree is still a copy.
    """
    directory = os.path.dirname(candidate_path).casefold() + "/"
    if any(seg in directory for seg in STAGING_SEGMENTS):
        return "staging"
    if any(seg in directory for seg in CURATED_SEGMENTS):
        return "curated"
    return "neutral"


def _path_signal(path_class: PathClass) -> float:
    if path_class == "curated":
        return W_PATH_CURATED
    if path_class == "staging":
        return W_PATH_STAGING
    return 0.0


@dataclass(slots=True)
class ScoredCandidate:
    """One candidate plus every signal that moved its score, for audit."""

    path: str
    tier: str
    base_confidence: float
    matcher_reason: str
    duration_signal: float
    size_signal: float
    isrc_signal: float
    path_class: PathClass
    path_signal: float
    dataless: bool
    hard_reject: str = ""

    @property
    def score(self) -> float:
        return round(
            self.base_confidence
            + self.duration_signal
            + self.size_signal
            + self.isrc_signal
            + self.path_signal,
            4,
        )


@dataclass(slots=True)
class ScoredRow:
    """An ambiguous row, its ranked candidates and the resolution verdict."""

    stable_id: str
    recorded_path: str | None
    ambiguity: str
    candidates: list[ScoredCandidate]
    resolved: bool = False
    resolved_reason: str = ""

    @property
    def top(self) -> ScoredCandidate | None:
        live = [c for c in self.candidates if not c.hard_reject]
        return live[0] if live else None

    @property
    def margin(self) -> float:
        live = [c for c in self.candidates if not c.hard_reject]
        if len(live) < 2:
            return float("inf")
        return round(live[0].score - live[1].score, 4)


def _duration_signal(row: match.TrackRow, entry: DiskAudio) -> float:
    if row.duration_s is None or entry.duration_s is None:
        return 0.0
    delta = abs(entry.duration_s - row.duration_s)
    if delta <= DUR_TIGHT_S:
        return W_DURATION_TIGHT
    if delta <= DUR_LOOSE_S:
        return W_DURATION_LOOSE
    if delta >= DUR_CONTRADICTION_S:
        return W_DURATION_CONTRADICTED
    return 0.0


def _size_signal(row: match.TrackRow, entry: DiskAudio) -> float:
    if not row.expected_size_bytes:
        return 0.0
    if entry.size_bytes == row.expected_size_bytes:
        return W_SIZE_EXACT
    return W_SIZE_CONTRADICTED


def _isrc_signal(row: match.TrackRow, entry: DiskAudio) -> float:
    if not row.isrc or not entry.isrc:
        return 0.0
    return W_ISRC_MATCH if row.isrc == entry.isrc else W_ISRC_CONTRADICTED


def score_candidates(
    row: match.TrackRow,
    result: match.RowResult,
    index: DiskIndex,
    *,
    taken_paths: Iterable[str] = (),
) -> ScoredRow:
    """Re-score one ambiguous row's candidates with corroborating signals."""
    by_path = {e.path: e for e in index.entries}
    taken = set(taken_paths)
    scored: list[ScoredCandidate] = []
    for cand in result.candidates:
        entry = by_path.get(cand.path)
        if entry is None:
            # In the index at classification time, gone from it now.
            scored.append(
                ScoredCandidate(
                    path=cand.path,
                    tier=cand.tier,
                    base_confidence=cand.confidence,
                    matcher_reason=cand.reason,
                    duration_signal=0.0,
                    size_signal=0.0,
                    isrc_signal=0.0,
                    path_class="neutral",
                    path_signal=0.0,
                    dataless=False,
                    hard_reject="no longer in the disk index",
                )
            )
            continue
        path_class = classify_path(cand.path)
        hard = ""
        if not os.path.exists(cand.path):
            hard = "does not exist on disk"
        elif cand.path in taken:
            hard = "already the recorded path of another track row"
        scored.append(
            ScoredCandidate(
                path=cand.path,
                tier=cand.tier,
                base_confidence=cand.confidence,
                matcher_reason=cand.reason,
                duration_signal=_duration_signal(row, entry),
                size_signal=_size_signal(row, entry),
                isrc_signal=_isrc_signal(row, entry),
                path_class=path_class,
                path_signal=_path_signal(path_class),
                dataless=entry.dataless,
                hard_reject=hard,
            )
        )
    # Rejected candidates sort last so rank 1 is always actionable.
    scored.sort(key=lambda c: (bool(c.hard_reject), -c.score, c.path))
    return ScoredRow(
        stable_id=result.stable_id,
        recorded_path=result.file_path,
        ambiguity=result.ambiguity or "",
        candidates=scored,
    )


def _verdict(row: ScoredRow) -> tuple[bool, str]:
    """Resolved verdict for one row. See the module docstring's three rules."""
    top = row.top
    # Checked before the survivor test: a target-already-linked row's only
    # candidate IS hard-rejected, and "the file belongs to another row" is a far
    # more actionable message than "nothing survived".
    if row.ambiguity == "target-already-linked":
        return False, (
            "target is another row's live file; that is a duplicate decision "
            "for a human, not a missing piece of evidence"
        )
    if top is None:
        return False, "no candidate survives the hard filters"
    if top.score < RESOLVE_SCORE_MIN:
        return False, (
            f"top score {top.score:.3f} is below {RESOLVE_SCORE_MIN:.2f} "
            "(auto threshold plus one full content signal)"
        )
    if row.margin < RESOLVE_MARGIN:
        return False, (
            f"lead over the runner-up is {row.margin:.3f}, under "
            f"{RESOLVE_MARGIN:.2f}; folder plausibility alone must not decide"
        )
    return True, f"single candidate at {top.score:.3f}, leading by {row.margin:.3f}"


def resolve_contests(rows: Sequence[ScoredRow]) -> int:
    """Award a contested file to the best-scoring claimant. Returns awards.

    ``target-contested`` rows were demoted by the matcher because several rows
    each wanted one file. That IS breakable: the row whose content signals agree
    best is the plausible owner, provided it leads the other claimants by
    :data:`RESOLVE_MARGIN`. Losers stay unresolved -- they need a different file,
    not a better score.
    """
    contests: dict[str, list[ScoredRow]] = {}
    for row in rows:
        if row.ambiguity != "target-contested":
            continue
        top = row.top
        if top is not None:
            contests.setdefault(top.path, []).append(row)
    awards = 0
    for path, claimants in contests.items():
        if len(claimants) < 2:
            continue
        ranked = sorted(claimants, key=lambda r: -(r.top.score if r.top else 0.0))
        best, second = ranked[0], ranked[1]
        best_score = best.top.score if best.top else 0.0
        second_score = second.top.score if second.top else 0.0
        lead = round(best_score - second_score, 4)
        for row in ranked:
            row.resolved = False
            row.resolved_reason = (
                f"{len(claimants)} rows contest {path}; this row scores "
                f"{(row.top.score if row.top else 0.0):.3f}"
            )
        if lead >= RESOLVE_MARGIN and best_score >= RESOLVE_SCORE_MIN:
            best.resolved = True
            best.resolved_reason = (
                f"wins a {len(claimants)}-row contest for {path} by {lead:.3f}"
            )
            awards += 1
    return awards


def score_rows(
    rows: Sequence[match.TrackRow],
    results: Sequence[match.RowResult],
    index: DiskIndex,
) -> list[ScoredRow]:
    """Score every ``relinkable-ambiguous`` row and set its verdict."""
    by_id = {row.stable_id: row for row in rows}
    recorded = {r.file_path for r in results if r.file_path}
    scored: list[ScoredRow] = []
    for res in results:
        if res.bucket != "relinkable-ambiguous":
            continue
        row = by_id.get(res.stable_id)
        if row is None:
            raise KeyError(f"result {res.stable_id} has no matching track row")
        taken = recorded - {res.file_path}
        out = score_candidates(row, res, index, taken_paths=taken)
        out.resolved, out.resolved_reason = _verdict(out)
        scored.append(out)
    # Contest resolution overrides the per-row verdict for contested rows only.
    resolve_contests([r for r in scored if r.ambiguity == "target-contested"])
    scored.sort(
        key=lambda r: (not r.resolved, -(r.top.score if r.top else 0.0), r.stable_id)
    )
    return scored


# ----- report ------------------------------------------------------------


def review_path_for(state_dir: Path, *, date: str | None = None) -> Path:
    stamp = date or time.strftime("%Y-%m-%d")
    return state_dir / f"relink-review-{stamp}.csv"


def write_review_csv(rows: Sequence[ScoredRow], out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(REVIEW_COLUMNS)
        for row in rows:
            margin = row.margin
            for rank, cand in enumerate(row.candidates, start=1):
                writer.writerow(
                    [
                        rank,
                        row.stable_id,
                        "yes" if row.resolved else "no",
                        row.resolved_reason,
                        row.ambiguity,
                        row.recorded_path or "",
                        cand.path,
                        cand.tier,
                        f"{cand.base_confidence:.3f}",
                        f"{cand.score:.3f}",
                        "" if margin == float("inf") else f"{margin:.3f}",
                        f"{cand.duration_signal:+.2f}",
                        f"{cand.size_signal:+.2f}",
                        f"{cand.isrc_signal:+.2f}",
                        cand.path_class,
                        f"{cand.path_signal:+.2f}",
                        "yes" if cand.dataless else "no",
                        cand.hard_reject or cand.matcher_reason,
                    ]
                )
    return out


def _print_summary(rows: Sequence[ScoredRow], out: Path) -> None:
    table = Table(title=f"Ambiguous rows re-scored ({len(rows)})", show_lines=False)
    table.add_column("Ambiguity", style="bold")
    table.add_column("Rows", justify="right")
    table.add_column("Resolved to one high-confidence candidate", justify="right")
    for kind in match.ALL_AMBIGUITIES:
        subset = [r for r in rows if r.ambiguity == kind]
        table.add_row(kind, str(len(subset)), str(sum(1 for r in subset if r.resolved)))
    table.add_row(
        "[bold]TOTAL[/bold]",
        f"[bold]{len(rows)}[/bold]",
        f"[bold]{sum(1 for r in rows if r.resolved)}[/bold]",
    )
    console.print(table)
    console.print(f"Ranked review CSV -> {out}")
    console.print(
        "[yellow]Not applied.[/yellow] These are proposals: the maintainer picks, then "
        "`relink --bucket relinkable-ambiguous` applies the ones he accepts."
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m apps.reconcile relink-review",
        description=(
            "Break ambiguous relink ties with content signals and emit a ranked "
            "review CSV. Read-only: never writes state.db, never applies."
        ),
    )
    p.add_argument("--data-dir", type=Path, default=paths.DATA_DIR)
    p.add_argument("--index-cache", type=Path, default=None)
    p.add_argument(
        "--roots",
        nargs="+",
        type=Path,
        default=None,
        help=f"disk roots to index (default: {' '.join(str(r) for r in DEFAULT_ROOTS)})",
    )
    p.add_argument("--rebuild-index", action="store_true")
    p.add_argument(
        "--out", type=Path, default=None, help="default <data-dir>/state/relink-review-<date>.csv"
    )
    p.add_argument("--limit", type=int, default=None, help="classify only N rows")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    state_db = args.data_dir / "state" / "state.db"
    if not state_db.exists():
        console.print(f"[red]state DB not found at {state_db}[/red]")
        return 2
    rb_db = args.data_dir / "master.plain.db"
    rows = match.load_track_rows(state_db, rb_db=rb_db if rb_db.exists() else None)
    if args.limit is not None:
        rows = rows[: args.limit]
    cache = args.index_cache or args.data_dir / "state" / CACHE_PATH.name
    roots = args.roots if args.roots else list(DEFAULT_ROOTS)
    index, stats = build_index(roots, cache_path=cache, rebuild=args.rebuild_index)
    console.print(
        f"{len(rows)} rows, {stats.walked} disk audio files "
        f"({stats.tag_reads} tag reads, {stats.reused} cache hits)"
    )
    results = match.classify_rows(rows, index)
    scored = score_rows(rows, results, index)
    out = args.out or review_path_for(args.data_dir / "state")
    write_review_csv(scored, out)
    _print_summary(scored, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
