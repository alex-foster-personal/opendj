"""``python -m apps.dedup.find_clusters`` -- group near-duplicates.

Reads the ``fingerprints`` cache (populated by :mod:`apps.dedup.scan`),
runs pairwise similarity above a threshold, single-linkage-clusters the
hits, and picks a canonical file per cluster using the tie-break ladder
from CONTEXT D2.

Outputs:

* ``data/dedup/clusters.csv``       -- cluster_id, canonical, alias rows.
* ``data/dedup/manual-review.csv``  -- borderline clusters that need eyes.
* Inserts rows into ``duplicate_clusters`` + ``track_aliases``.

The clustering is O(N^2) in the number of fingerprints; for a 1,700-
track library that is ~1.4M compares, each a ~1 microsecond XOR loop,
so sub-3s end-to-end. No need for LSH at this scale.
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from apps.shared import paths
from apps.shared.fingerprints import Fingerprint, FingerprintCache, compare

from . import schema as dedup_schema

DEFAULT_THRESHOLD = 0.92
DEFAULT_MAX_CLUSTER = 8
DEFAULT_DURATION_DELTA_S = 3.0


# -------------------------------------------------------- Union-Find helper


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]  # halving
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


# ----------------------------------------------------- canonical selection


@dataclass(frozen=True, slots=True)
class _MemberRow:
    fp: Fingerprint
    # Original library root of the file (first ``MUSIC_ROOTS`` entry that
    # matches); used in the tie-break ladder.
    on_canonical_root: bool


def _pick_canonical(members: list[_MemberRow]) -> tuple[_MemberRow, str]:
    """Apply the D2 tie-break ladder; return ``(winner, rationale)``.

    Order:
      1. Highest bitrate.
      2. Largest file size.
      3. Longest duration.
      4. File on the canonical library root.
      5. Oldest mtime.
    """
    if not members:
        raise ValueError("no members")
    if len(members) == 1:
        return members[0], "only-member"

    # Step 1: highest bitrate (None treated as 0).
    best_bitrate = max(m.fp.bitrate or 0 for m in members)
    cands = [m for m in members if (m.fp.bitrate or 0) == best_bitrate]
    if len(cands) == 1:
        return cands[0], f"bitrate={best_bitrate}"

    # Step 2: largest size.
    best_size = max(m.fp.size for m in cands)
    cands = [m for m in cands if m.fp.size == best_size]
    if len(cands) == 1:
        return cands[0], f"size={best_size}"

    # Step 3: longest duration.
    best_dur = max(m.fp.duration for m in cands)
    cands = [m for m in cands if abs(m.fp.duration - best_dur) < 0.001]
    if len(cands) == 1:
        return cands[0], f"duration={best_dur:.2f}"

    # Step 4: canonical library root.
    on_root = [m for m in cands if m.on_canonical_root]
    if on_root and len(on_root) < len(cands):
        cands = on_root
        if len(cands) == 1:
            return cands[0], "on-canonical-root"

    # Step 5: oldest mtime.
    cands.sort(key=lambda m: m.fp.mtime)
    return cands[0], f"oldest-mtime={cands[0].fp.mtime:.0f}"


def _wipe_derived_cluster_tables(conn: sqlite3.Connection) -> None:
    """Derived tables are rebuilt each run; fingerprints stay."""
    conn.execute("DELETE FROM track_aliases")
    conn.execute("DELETE FROM duplicate_clusters")
    sequence_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'"
    ).fetchone()
    if sequence_exists is not None:
        conn.execute(
            "DELETE FROM sqlite_sequence WHERE name = 'duplicate_clusters'"
        )


def _is_on_canonical_root(path: Path, roots: Iterable[Path]) -> bool:
    for r in roots:
        try:
            path.resolve().relative_to(r.resolve())
            return True
        except ValueError:
            continue
    return False


# -------------------------------------------------------------- clustering


@dataclass(slots=True)
class ClusterOutcome:
    cluster_id: int
    canonical_path: str
    canonical_stable_id: str
    alias_paths: list[str]
    alias_stable_ids: list[str]
    similarities: list[float]
    rationale: str
    flagged_manual_review: bool = False


def _stable_id_from_row(conn: sqlite3.Connection, path: str) -> str:
    row = conn.execute(
        "SELECT stable_id FROM fingerprints WHERE path = ?", (path,)
    ).fetchone()
    return row[0] if row and row[0] else ""


def run_find_clusters(
    *,
    db_path: Path | None = None,
    threshold: float = DEFAULT_THRESHOLD,
    max_cluster_size: int = DEFAULT_MAX_CLUSTER,
    duration_delta_s: float = DEFAULT_DURATION_DELTA_S,
    roots: list[Path] | None = None,
    clusters_csv: Path | None = None,
    manual_review_csv: Path | None = None,
    fingerprints: list[Fingerprint] | None = None,
) -> list[ClusterOutcome]:
    """Programmatic clustering; used by the CLI and tests."""
    use_db = db_path if db_path is not None else paths.DEDUP_FALLBACK_DB
    use_roots = roots if roots is not None else paths.MUSIC_ROOTS
    use_csv = clusters_csv if clusters_csv is not None else paths.DEDUP_CLUSTERS_CSV
    use_manual = (
        manual_review_csv
        if manual_review_csv is not None
        else paths.DEDUP_MANUAL_REVIEW_CSV
    )

    # Read fingerprints from cache (or take the provided list for tests).
    if fingerprints is None:
        cache = FingerprintCache(use_db)
        fps = list(cache.iter_all())
    else:
        fps = list(fingerprints)

    n = len(fps)
    if n == 0:
        use_csv.parent.mkdir(parents=True, exist_ok=True)
        use_csv.write_text("cluster_id,canonical_path\n", encoding="utf-8")
        return []

    uf = _UnionFind(n)
    pair_similarity: dict[tuple[int, int], float] = {}
    for i in range(n):
        for j in range(i + 1, n):
            sim = compare(fps[i], fps[j])
            if sim < threshold:
                continue
            # Duration-delta guard: flag high sim + large delta as
            # manual review (will still cluster, but marked).
            uf.union(i, j)
            key = (min(i, j), max(i, j))
            pair_similarity[key] = sim

    # Group members by root.
    buckets: dict[int, list[int]] = {}
    for idx in range(n):
        root = uf.find(idx)
        buckets.setdefault(root, []).append(idx)

    # Open state DB for the write-through (INSERT rows into
    # duplicate_clusters / track_aliases).
    conn = dedup_schema.ensure_schema(use_db)
    _wipe_derived_cluster_tables(conn)

    outcomes: list[ClusterOutcome] = []
    use_csv.parent.mkdir(parents=True, exist_ok=True)
    use_manual.parent.mkdir(parents=True, exist_ok=True)

    with open(use_csv, "w", newline="", encoding="utf-8") as csv_f, open(
        use_manual, "w", newline="", encoding="utf-8"
    ) as manual_f:
        w = csv.writer(csv_f)
        m = csv.writer(manual_f)
        w.writerow(
            [
                "cluster_id",
                "canonical_path",
                "canonical_stable_id",
                "alias_path",
                "alias_stable_id",
                "similarity",
                "rationale",
            ]
        )
        m.writerow(
            [
                "cluster_id",
                "reason",
                "canonical_path",
                "alias_path",
                "similarity",
                "duration_delta_s",
            ]
        )

        for members_idx in buckets.values():
            if len(members_idx) < 2:
                continue
            members = [
                _MemberRow(
                    fp=fps[i],
                    on_canonical_root=_is_on_canonical_root(fps[i].path, use_roots),
                )
                for i in members_idx
            ]
            winner, rationale = _pick_canonical(members)
            winner_path = str(winner.fp.path)
            canonical_sid = _stable_id_from_row(conn, winner_path)
            oversized_cluster = len(members) > max_cluster_size

            # Write cluster header row.
            cur = conn.execute(
                "INSERT INTO duplicate_clusters "
                "(canonical_stable_id, canonical_path, rationale, "
                " flagged_manual_review) "
                "VALUES (?, ?, ?, ?)",
                (canonical_sid, winner_path, rationale, int(oversized_cluster)),
            )
            cluster_id = cur.lastrowid or 0

            flagged_manual_review = oversized_cluster
            alias_paths: list[str] = []
            alias_sids: list[str] = []
            sims: list[float] = []

            for m_row in members:
                if m_row.fp.path == winner.fp.path:
                    continue
                apath = str(m_row.fp.path)
                asid = _stable_id_from_row(conn, apath)
                # Similarity between alias and canonical.
                sim = compare(winner.fp, m_row.fp)
                dur_delta = abs(winner.fp.duration - m_row.fp.duration)
                conn.execute(
                    "INSERT OR REPLACE INTO track_aliases "
                    "(alias_stable_id, alias_path, cluster_id, "
                    " canonical_stable_id, similarity) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (asid, apath, cluster_id, canonical_sid, sim),
                )
                w.writerow(
                    [
                        cluster_id,
                        winner_path,
                        canonical_sid,
                        apath,
                        asid,
                        f"{sim:.4f}",
                        rationale,
                    ]
                )
                alias_paths.append(apath)
                alias_sids.append(asid)
                sims.append(sim)
                # Manual-review trigger: duration delta > threshold_s.
                if dur_delta > duration_delta_s or oversized_cluster:
                    flagged_manual_review = True
                    reason = (
                        "max_cluster"
                        if oversized_cluster
                        else f"duration_delta={dur_delta:.2f}s"
                    )
                    m.writerow(
                        [
                            cluster_id,
                            reason,
                            winner_path,
                            apath,
                            f"{sim:.4f}",
                            f"{dur_delta:.2f}",
                        ]
                    )

            conn.execute(
                "UPDATE duplicate_clusters SET flagged_manual_review = ? "
                "WHERE cluster_id = ?",
                (int(flagged_manual_review), cluster_id),
            )

            outcomes.append(
                ClusterOutcome(
                    cluster_id=cluster_id,
                    canonical_path=winner_path,
                    canonical_stable_id=canonical_sid,
                    alias_paths=alias_paths,
                    alias_stable_ids=alias_sids,
                    similarities=sims,
                    rationale=rationale,
                    flagged_manual_review=flagged_manual_review,
                )
            )

    conn.commit()
    conn.close()
    return outcomes


# ---------------------------------------------------------------- CLI


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m apps.dedup.find_clusters",
        description="Cluster near-duplicate tracks (Phase 7 dedup).",
    )
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    p.add_argument("--max-cluster-size", type=int, default=DEFAULT_MAX_CLUSTER)
    p.add_argument("--duration-delta-s", type=float, default=DEFAULT_DURATION_DELTA_S)
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--clusters-csv", type=Path, default=None)
    p.add_argument("--manual-review-csv", type=Path, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    outcomes = run_find_clusters(
        db_path=args.db,
        threshold=args.threshold,
        max_cluster_size=args.max_cluster_size,
        duration_delta_s=args.duration_delta_s,
        clusters_csv=args.clusters_csv,
        manual_review_csv=args.manual_review_csv,
    )
    total_aliases = sum(len(c.alias_paths) for c in outcomes)
    flagged = sum(1 for c in outcomes if c.flagged_manual_review)
    print(
        f"clusters={len(outcomes)} aliases={total_aliases} "
        f"flagged={flagged} threshold={args.threshold}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
