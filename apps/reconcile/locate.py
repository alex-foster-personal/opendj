"""Locate filesystem candidates for each broken RB row, with triple-validation.

Entry point::

    python -m apps.reconcile.locate

Reads ``data/reconcile/broken.csv`` (produced by :mod:`apps.reconcile.list_broken`),
indexes every audio file under :data:`paths.MUSIC_ROOTS`, and for each broken
row scores candidate replacements against six **independent signals** (each
contributes a fixed weight). A candidate is "triple-validated" when ≥3 signals
fire — only those are eligible for automatic apply later.

Signals (weight → key):
  1. basename exact (NFC, case-insensitive)          .35  ``basename_exact``
  2. basename fuzzy (SequenceMatcher ratio ≥0.85)    .20  ``basename_fuzzy``
  3. size within 1%                                  .20  ``size_match``
  4. path-rewrite heuristic (BACKUP prefix fixup)    .25  ``path_rewrite``
  5. ID3 title + artist match (tinytag, ≥0.9 ratio)  .15  ``id3_match``
  6. duration within ±0.5s                           .10  ``duration_match``
  7. audio fingerprint matches the one the duplicate
     scan recorded before the file went missing      .35  ``fingerprint_match``

A fingerprint also SEEDS candidates (a file that moved and was renamed, so
no basename fires) and can VETO one: ``fingerprint_mismatch`` (clearly
different audio under the same name) is listed among the signals but has no
weight and keeps the candidate from being triple-validated. Fingerprint
evidence exists only for tracks a duplicate scan fingerprinted; see
:mod:`apps.reconcile.fingerprint_evidence`.

Writes ``data/reconcile/located.csv``. Read-only: does not touch ``master.db``
and does not modify any file on disk.
"""
from __future__ import annotations

import csv
import difflib
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.table import Table

from apps.reconcile import list_broken as lb
from apps.reconcile.fingerprint_evidence import FingerprintEvidence
from apps.shared import audio_files, fs_residency, icloud_zone, paths

console = Console(width=120)

IN_CSV: Path = lb.OUT_CSV
OUT_CSV: Path = paths.DATA_DIR / "reconcile" / "located.csv"

# Signal weights. Tweak carefully: matching acceptance requires multiple
# independent signals; a private corpus tally is not public evidence.
SIGNAL_WEIGHTS: dict[str, float] = {
    "basename_exact": 0.35,
    "basename_fuzzy": 0.20,
    "size_match": 0.20,
    "path_rewrite": 0.25,
    "id3_match": 0.15,
    "duration_match": 0.10,
    "fingerprint_match": 0.35,
}

# Signals that rule a candidate out of automatic apply, whatever else fired.
VETO_SIGNALS: frozenset[str] = frozenset({"fingerprint_mismatch"})

# Deployment-specific legacy path rewrites are unavailable in this public copy.
# Other matching signals retain their configured weights and thresholds.
PATH_REWRITES: tuple[tuple[str, str], ...] = ()

BASENAME_FUZZY_THRESHOLD = 0.85
ID3_FUZZY_THRESHOLD = 0.9
SIZE_TOLERANCE = 0.01  # 1 %
DURATION_TOLERANCE_S = 0.5


@dataclass(slots=True)
class Candidate:
    """A scored match for a single broken RB row."""

    path: Path
    confidence: float = 0.0
    signals: list[str] = field(default_factory=list)

    @property
    def signal_count(self) -> int:
        return len(self.signals)

    @property
    def vetoed(self) -> bool:
        return any(s in VETO_SIGNALS for s in self.signals)

    @property
    def triple_validated(self) -> bool:
        if self.vetoed:
            return False
        return sum(1 for s in self.signals if s not in VETO_SIGNALS) >= 3


# ------------------------------------------------------------------ helpers


def _nfc_lower(s: str) -> str:
    return unicodedata.normalize("NFC", s).casefold()


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _load_broken() -> list[dict[str, str]]:
    if not IN_CSV.exists():
        console.print(
            f"[red]Missing {IN_CSV}. Run `python -m apps.reconcile.list_broken` first.[/red]"
        )
        raise SystemExit(1)
    with IN_CSV.open("r", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ------------------------------------------------------------------ index


@dataclass(slots=True)
class FsIndex:
    """Filesystem audio index — cheap lookups by normalized basename."""

    by_basename: dict[str, list[audio_files.AudioFile]]
    all_files: list[audio_files.AudioFile]

    @classmethod
    def build(cls, files: Iterable[audio_files.AudioFile]) -> "FsIndex":
        by_name: dict[str, list[audio_files.AudioFile]] = {}
        all_files: list[audio_files.AudioFile] = []
        for af in files:
            all_files.append(af)
            by_name.setdefault(_nfc_lower(af.path.name), []).append(af)
        return cls(by_basename=by_name, all_files=all_files)


# ------------------------------------------------------------------ scoring


def _score_candidate(
    cand_path: Path,
    cand_size: int | None,
    row: dict[str, str],
    id3: audio_files.AudioMetadata | None,
    signals: set[str],
) -> Candidate:
    """Sum weights from ``signals`` and build a ``Candidate`` record.

    ``id3`` is pre-read so duration/tag signals can be evaluated without
    re-opening the file.
    """
    # ID3 tag match signal.
    rb_title = (row.get("title") or "").strip()
    rb_artist = (row.get("artist") or "").strip()
    if id3 is not None and rb_title and rb_artist:
        id3_title = (id3.title or "").strip()
        id3_artist = (id3.artist or "").strip()
        if id3_title and id3_artist:
            t_ratio = _ratio(_nfc_lower(rb_title), _nfc_lower(id3_title))
            a_ratio = _ratio(_nfc_lower(rb_artist), _nfc_lower(id3_artist))
            if t_ratio >= ID3_FUZZY_THRESHOLD and a_ratio >= ID3_FUZZY_THRESHOLD:
                signals.add("id3_match")

    # Duration signal (pyrekordbox stores Length in seconds; broken.csv is int).
    rb_dur_raw = row.get("duration_s") or ""
    if id3 is not None and id3.duration_s is not None and rb_dur_raw:
        try:
            rb_dur = float(rb_dur_raw)
            if abs(id3.duration_s - rb_dur) <= DURATION_TOLERANCE_S:
                signals.add("duration_match")
        except ValueError:
            pass

    # Size signal.
    rb_size_raw = row.get("file_size") or ""
    if cand_size is not None and rb_size_raw:
        try:
            rb_size = int(rb_size_raw)
            if rb_size > 0:
                diff = abs(cand_size - rb_size) / rb_size
                if diff <= SIZE_TOLERANCE:
                    signals.add("size_match")
        except ValueError:
            pass

    conf = sum(SIGNAL_WEIGHTS[s] for s in signals if s not in VETO_SIGNALS)
    return Candidate(path=cand_path, confidence=conf, signals=sorted(signals))


def _rationale(signals: list[str], size_bytes: int | None) -> str:
    """Short human-readable explanation ordered like the roadmap example."""
    labels = {
        "basename_exact": "basename exact",
        "basename_fuzzy": "basename fuzzy",
        "size_match": (
            f"size match ({size_bytes:,})" if size_bytes else "size match"
        ),
        "path_rewrite": "path-rewrite heuristic applied",
        "id3_match": "id3 title+artist match",
        "duration_match": "duration match",
        "fingerprint_match": "same audio as the recorded fingerprint",
        "fingerprint_mismatch": "DIFFERENT audio from the recorded fingerprint",
    }
    return "; ".join(labels.get(s, s) for s in signals)


def _seed_by_fingerprint(
    row: dict[str, str],
    fingerprints: FingerprintEvidence | None,
    seeded: dict[Path, set[str]],
) -> str | None:
    """Add files carrying the track's recorded audio to ``seeded``; return
    the recorded fingerprint (None when no scan ever fingerprinted it)."""
    if fingerprints is None:
        return None
    original = row.get("original_path") or ""
    # broken.csv names no stable_id: the scan's own record of the track at
    # its old path does, so its moved file, recorded under it, still seeds.
    own = row.get("stable_id") or fingerprints.owner(original)
    recorded = fingerprints.recorded(original, own)
    if recorded is not None:
        for path in fingerprints.seeds(recorded, exclude=original, stable_id=own):
            seeded.setdefault(path, set()).add("fingerprint_match")
    return recorded


def _add_fingerprint_verdict(
    sigs: set[str],
    recorded: str | None,
    fingerprints: FingerprintEvidence | None,
    cand_path: Path,
) -> None:
    """Fingerprint a name-seeded candidate: match, mismatch, or no verdict."""
    if recorded is None or fingerprints is None or "fingerprint_match" in sigs:
        return
    verdict = fingerprints.signal(recorded, cand_path)
    if verdict is not None:
        sigs.add(verdict)


def _locate_candidates(
    row: dict[str, str],
    index: FsIndex,
    id3_cache: dict[Path, audio_files.AudioMetadata | None],
    fingerprints: FingerprintEvidence | None = None,
) -> list[Candidate]:
    """Build + score every candidate for a single row, best first.

    Shared core for :func:`_locate_one` (CLI: wants only the winner) and
    :func:`find_candidates` (webui relocate-files: wants the ranked list so
    a human can pick a runner-up). Returns ``[]`` when nothing seeds a
    candidate (no exact/fuzzy basename hit, no path-rewrite hit).
    """
    original = row.get("original_path") or ""
    basename = row.get("basename") or Path(original).name
    if not basename:
        return []

    # --- collect path candidates + the signals they already satisfy.
    # Map path → signals-triggered-by-presence (basename_exact / path_rewrite
    # / basename_fuzzy). Size / id3 / duration are scored later per-file.
    seeded: dict[Path, set[str]] = {}

    key = _nfc_lower(basename)
    # (1) basename exact.
    for af in index.by_basename.get(key, ()):
        seeded.setdefault(af.path, set()).add("basename_exact")

    # (4) path-rewrite heuristic — try each configured prefix rewrite.
    for src_prefix, dst_prefix in PATH_REWRITES:
        if original.startswith(src_prefix):
            rewritten = Path(dst_prefix + original[len(src_prefix):])
            if rewritten.exists():
                seeded.setdefault(rewritten, set()).add("path_rewrite")

    # (2) basename fuzzy — only if no exact candidate yet, to avoid fuzzy
    # adding noise to every exact match. Scan all indexed basenames.
    if "basename_exact" not in {s for sigs in seeded.values() for s in sigs}:
        best_ratio = 0.0
        best_af: audio_files.AudioFile | None = None
        for cand_name, afs in index.by_basename.items():
            r = _ratio(key, cand_name)
            if r >= BASENAME_FUZZY_THRESHOLD and r > best_ratio:
                best_ratio = r
                best_af = afs[0]
        if best_af is not None:
            seeded.setdefault(best_af.path, set()).add("basename_fuzzy")

    # (7) fingerprint: the audio the track had, wherever it lives now.
    recorded = _seed_by_fingerprint(row, fingerprints, seeded)

    if not seeded:
        return []

    # Score each seeded path (read id3 lazily, cache results).
    # Prefer durable Music roots: drop iCloud-zone and dataless stubs.
    candidates: list[Candidate] = []
    fs_by_path = {af.path: af for af in index.all_files}
    for cand_path, sigs in seeded.items():
        if icloud_zone.is_icloud_zone(cand_path):
            continue
        if not fs_residency.is_materialised(cand_path):
            continue
        af = fs_by_path.get(cand_path)
        size = af.size_bytes if af else None
        if cand_path not in id3_cache:
            id3_cache[cand_path] = audio_files.read_metadata(cand_path)
        id3 = id3_cache[cand_path]
        _add_fingerprint_verdict(sigs, recorded, fingerprints, cand_path)
        cand = _score_candidate(cand_path, size, row, id3, sigs)
        candidates.append(cand)

    # Best = not vetoed, then highest confidence, then most signals, then
    # shortest path.
    candidates.sort(
        key=lambda c: (c.vetoed, -c.confidence, -c.signal_count, len(str(c.path)))
    )
    return candidates


def _locate_one(
    row: dict[str, str],
    index: FsIndex,
    id3_cache: dict[Path, audio_files.AudioMetadata | None],
    fingerprints: FingerprintEvidence | None = None,
) -> Candidate | None:
    """Build candidate set for a single row and return the best one (or None)."""
    candidates = _locate_candidates(row, index, id3_cache, fingerprints)
    return candidates[0] if candidates else None


def find_candidates(
    row: dict[str, str],
    index: FsIndex,
    id3_cache: dict[Path, audio_files.AudioMetadata | None],
    limit: int = 5,
    fingerprints: FingerprintEvidence | None = None,
) -> list[Candidate]:
    """Ranked candidates for ``row`` (best first), capped at ``limit``.

    Public entry point for callers that want more than the single best
    match -- e.g. the webui relocate-files feature, which lets a human pick
    among the top few rather than only ever seeing the winner.
    """
    return _locate_candidates(row, index, id3_cache, fingerprints)[:limit]


# ------------------------------------------------------------------ I/O


OUT_COLUMNS: tuple[str, ...] = (
    "id",
    "title",
    "artist",
    "original_path",
    "best_candidate_path",
    "confidence",
    "signals_hit",
    "signal_count",
    "rationale",
    "triple_validated",
)


def _write_output(rows: list[dict[str, str]]) -> None:
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=OUT_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _print_summary(out_rows: list[dict[str, str]], total: int) -> None:
    validated = [r for r in out_rows if r["triple_validated"] == "True"]
    sub_three = [
        r
        for r in out_rows
        if r["best_candidate_path"] and r["triple_validated"] == "False"
    ]
    no_cand = [r for r in out_rows if not r["best_candidate_path"]]
    avg_v = (
        sum(float(r["confidence"]) for r in validated) / len(validated)
        if validated
        else 0.0
    )

    table = Table(title="Locate summary", show_lines=False)
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("Broken rows", f"{total}")
    table.add_row("Triple-validated (≥3 signals)", f"[green]{len(validated)}[/green]")
    table.add_row("Has candidate but <3 signals", f"[yellow]{len(sub_three)}[/yellow]")
    table.add_row("No candidate", f"[red]{len(no_cand)}[/red]")
    table.add_row("Avg confidence (validated)", f"{avg_v:.3f}")
    console.print(table)


def main() -> None:
    console.print(f"[bold]Step 1:[/bold] reading {IN_CSV}")
    broken = _load_broken()
    console.print(f"  • {len(broken)} broken rows")

    console.print(f"[bold]Step 2:[/bold] indexing {paths.MUSIC_ROOTS}")
    index = FsIndex.build(audio_files.scan_music_files())
    console.print(
        f"  • {len(index.all_files)} audio files "
        f"({len(index.by_basename)} unique basenames)"
    )

    fingerprints = FingerprintEvidence.open()
    if fingerprints is None:
        console.print("  • no duplicate scan database: fingerprint signal unavailable")
    else:
        console.print(f"  • {len(fingerprints.by_path)} recorded fingerprints from {fingerprints.db_path}")

    console.print("[bold]Step 3:[/bold] scoring candidates")
    id3_cache: dict[Path, audio_files.AudioMetadata | None] = {}
    out_rows: list[dict[str, str]] = []
    for row in broken:
        best = _locate_one(row, index, id3_cache, fingerprints)
        if best is None:
            out_rows.append(
                {
                    "id": row["id"],
                    "title": row.get("title", ""),
                    "artist": row.get("artist", ""),
                    "original_path": row.get("original_path", ""),
                    "best_candidate_path": "",
                    "confidence": "0.0",
                    "signals_hit": "",
                    "signal_count": "0",
                    "rationale": "no candidate found",
                    "triple_validated": "False",
                }
            )
            continue
        # Reuse the fs AudioFile for a nicer rationale (size).
        size = None
        for af in index.all_files:
            if af.path == best.path:
                size = af.size_bytes
                break
        out_rows.append(
            {
                "id": row["id"],
                "title": row.get("title", ""),
                "artist": row.get("artist", ""),
                "original_path": row.get("original_path", ""),
                "best_candidate_path": str(best.path),
                "confidence": f"{best.confidence:.3f}",
                "signals_hit": "|".join(best.signals),
                "signal_count": str(best.signal_count),
                "rationale": _rationale(best.signals, size),
                "triple_validated": "True" if best.triple_validated else "False",
            }
        )

    out_rows.sort(key=lambda r: -float(r["confidence"]))

    console.print(f"[bold]Step 4:[/bold] writing {OUT_CSV}")
    _write_output(out_rows)

    _print_summary(out_rows, len(broken))
    if fingerprints is not None and fingerprints.unmeasured:
        console.print(
            f"[yellow]{fingerprints.unmeasured} candidate(s) could not be fingerprinted; "
            "their fingerprint signal is UNKNOWN, not a mismatch.[/yellow]"
        )


if __name__ == "__main__":
    main()
