"""Audio-reference, free-text, and hybrid semantic retrieval (issue #11247).

Distinct from FTS5 metadata search in ``search_index.py`` and from DJ compatibility
scoring (issue #2419). Embedding-ranked rows are candidate neighbours only.

Local baseline: CLAP vectors on disk (``apps.genre_infer.store``) and persisted
``enrichment_claims``; no vendor account is required.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from apps.genre_infer import findings, groups, store
from apps.genre_infer.claim_envelope import EnrichmentClaim

RetrievalMode = Literal["audio_reference", "free_text", "hybrid"]
FactorKind = Literal["tag", "embedding", "filter"]
RankingKind = Literal["embedding_candidate"]

SIMILARITY_QUALITY_MIN_TRACKS = 1000
LOCAL_BASELINE = "local/claims-and-embeddings"
CANDIDATE_RANKING: RankingKind = "embedding_candidate"

_TOKEN = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True, slots=True)
class RetrievalFactor:
    kind: FactorKind
    score: float
    detail: str


@dataclass(frozen=True, slots=True)
class SemanticFilters:
    bpm_min: float | None = None
    bpm_max: float | None = None
    genre: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "bpm_min": self.bpm_min,
            "bpm_max": self.bpm_max,
            "genre": self.genre,
        }


@dataclass(frozen=True, slots=True)
class RetrievalReport:
    retrieval_mode: RetrievalMode
    baseline: str
    indexed_embedding_tracks: int
    indexed_claim_tracks: int
    similarity_quality_claimed: bool
    similarity_quality_note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "retrieval_mode": self.retrieval_mode,
            "baseline": self.baseline,
            "indexed_embedding_tracks": self.indexed_embedding_tracks,
            "indexed_claim_tracks": self.indexed_claim_tracks,
            "similarity_quality_claimed": self.similarity_quality_claimed,
            "similarity_quality_note": self.similarity_quality_note,
        }


@dataclass(frozen=True, slots=True)
class SemanticCandidate:
    stable_id: str
    ranking_kind: RankingKind
    factors: tuple[RetrievalFactor, ...]
    distance: float | None = None


@dataclass(frozen=True, slots=True)
class SemanticRetrievalResult:
    mode: RetrievalMode
    report: RetrievalReport
    items: tuple[SemanticCandidate, ...]


def _tokens(text: str) -> frozenset[str]:
    return frozenset(_TOKEN.findall(text.lower()))


def _unit(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm <= 0:
        raise ValueError("embedding vector has zero norm")
    return [v / norm for v in vec]


def _cosine(a: list[float], b: list[float]) -> float:
    ua, ub = _unit(a), _unit(b)
    return round(sum(x * y for x, y in zip(ua, ub, strict=True)), 6)


def _embeddings_for_live(data_dir: Path, live: frozenset[str]) -> dict[str, list[float]]:
    vectors, _failures, _model, _revision = store.load_embeddings(data_dir)
    return {sid: vec for sid, vec in vectors.items() if sid in live}


def _claims_for_live(conn, live: frozenset[str]) -> dict[str, list[EnrichmentClaim]]:
    grouped = findings.claims_grouped(conn)
    return {sid: rows for sid, rows in grouped.items() if sid in live}


def _track_field(conn, stable_id: str, field_name: str) -> object | None:
    row = conn.execute(
        "SELECT value_json FROM track_fields WHERE stable_id = ? AND field_name = ? "
        "AND deleted_at IS NULL ORDER BY modified_at DESC LIMIT 1",
        (stable_id, field_name),
    ).fetchone()
    if row is None:
        return None
    return json.loads(row[0])


def _track_bpm_and_genre(conn, stable_id: str) -> tuple[float | None, str | None]:
    live = conn.execute(
        "SELECT 1 FROM tracks WHERE stable_id = ? AND deleted_at IS NULL",
        (stable_id,),
    ).fetchone()
    if live is None:
        return None, None
    bpm_raw = _track_field(conn, stable_id, "bpm")
    genre_raw = _track_field(conn, stable_id, "genre")
    bpm = float(bpm_raw) if isinstance(bpm_raw, (int, float)) else None
    genre = str(genre_raw) if isinstance(genre_raw, str) and genre_raw.strip() else None
    return bpm, genre


def _filter_factor(
    conn,
    stable_id: str,
    *,
    filters: SemanticFilters,
    tag_filters: dict[str, str],
    claims: dict[str, list[EnrichmentClaim]],
) -> RetrievalFactor | None:
    bpm, genre = _track_bpm_and_genre(conn, stable_id)
    if filters.bpm_min is not None and (bpm is None or bpm < filters.bpm_min):
        return None
    if filters.bpm_max is not None and (bpm is None or bpm > filters.bpm_max):
        return None
    if filters.genre is not None and (genre is None or genre.lower() != filters.genre.lower()):
        return None
    for dimension, want in tag_filters.items():
        track_claims = claims.get(stable_id, ())
        matched = any(
            c.dimension == dimension and str(c.value).lower() == want.lower()
            for c in track_claims
            if c.kind == "semantic_tag"
        )
        if not matched:
            return None
    detail_parts: list[str] = []
    if filters.bpm_min is not None or filters.bpm_max is not None:
        detail_parts.append(f"bpm={bpm}")
    if filters.genre is not None:
        detail_parts.append(f"genre={genre!r}")
    for dimension, want in tag_filters.items():
        detail_parts.append(f"{dimension}={want!r}")
    detail = "passed: " + (", ".join(detail_parts) if detail_parts else "no structural filters")
    return RetrievalFactor(kind="filter", score=1.0, detail=detail)


def _tag_factor(claims: list[EnrichmentClaim], tag_filters: dict[str, str]) -> RetrievalFactor:
    if not tag_filters:
        hits = sum(1 for c in claims if c.kind == "semantic_tag")
        score = min(1.0, hits / 3.0) if hits else 0.0
        return RetrievalFactor(kind="tag", score=round(score, 4), detail=f"{hits} semantic tag claims")
    matched = 0
    for dimension, want in tag_filters.items():
        if any(
            c.dimension == dimension and str(c.value).lower() == want.lower()
            for c in claims
            if c.kind == "semantic_tag"
        ):
            matched += 1
    score = matched / len(tag_filters)
    return RetrievalFactor(
        kind="tag",
        score=round(score, 4),
        detail=f"matched {matched} of {len(tag_filters)} tag filters",
    )


def _text_factor(claims: list[EnrichmentClaim], query: str) -> RetrievalFactor:
    q = _tokens(query)
    if not q:
        return RetrievalFactor(kind="tag", score=0.0, detail="empty query")
    corpus = findings.claim_search_text(claims)
    hit = len(q & _tokens(corpus))
    score = hit / len(q) if q else 0.0
    return RetrievalFactor(kind="tag", score=round(score, 4), detail=f"token overlap {hit}/{len(q)} on claim text")


def build_retrieval_report(
    mode: RetrievalMode,
    *,
    embedding_count: int,
    claim_count: int,
) -> RetrievalReport:
    if embedding_count >= SIMILARITY_QUALITY_MIN_TRACKS:
        note = (
            f"similarity quality may be assessed with {embedding_count} embedded tracks "
            f"(minimum advisory threshold {SIMILARITY_QUALITY_MIN_TRACKS})"
        )
        claimed = True
    else:
        note = (
            f"similarity quality is not claimed: {embedding_count} embedded tracks "
            f"(advisory minimum {SIMILARITY_QUALITY_MIN_TRACKS}; Cyanite guidance)"
        )
        claimed = False
    return RetrievalReport(
        retrieval_mode=mode,
        baseline=LOCAL_BASELINE,
        indexed_embedding_tracks=embedding_count,
        indexed_claim_tracks=claim_count,
        similarity_quality_claimed=claimed,
        similarity_quality_note=note,
    )


def _rank_key(candidate: SemanticCandidate) -> tuple[float, str]:
    total = sum(f.score for f in candidate.factors)
    return (-total, candidate.stable_id)


def audio_reference_retrieval(
    conn,
    data_dir: Path,
    *,
    reference_stable_id: str,
    limit: int = 20,
    filters: SemanticFilters | None = None,
    tag_filters: dict[str, str] | None = None,
) -> SemanticRetrievalResult:
    """Nearest-neighbour retrieval from a reference track's stored embedding."""
    live = groups.live_stable_ids(conn)
    embeddings = _embeddings_for_live(data_dir, live)
    claims = _claims_for_live(conn, live)
    report = build_retrieval_report(
        "audio_reference",
        embedding_count=len(embeddings),
        claim_count=len(claims),
    )
    filt = filters or SemanticFilters()
    tags = tag_filters or {}
    if reference_stable_id not in embeddings:
        return SemanticRetrievalResult(mode="audio_reference", report=report, items=())
    target = embeddings[reference_stable_id]
    scored: list[SemanticCandidate] = []
    for sid, vec in embeddings.items():
        if sid == reference_stable_id:
            continue
        filter_factor = _filter_factor(conn, sid, filters=filt, tag_filters=tags, claims=claims)
        if filter_factor is None:
            continue
        distance = round(1.0 - _cosine(target, vec), 6)
        emb_score = round(1.0 - distance, 4)
        factors = (
            filter_factor,
            RetrievalFactor(kind="embedding", score=emb_score, detail="cosine distance to reference"),
        )
        scored.append(
            SemanticCandidate(
                stable_id=sid,
                ranking_kind=CANDIDATE_RANKING,
                factors=factors,
                distance=distance,
            )
        )
    scored.sort(key=lambda c: (c.distance if c.distance is not None else 1.0, c.stable_id))
    return SemanticRetrievalResult(mode="audio_reference", report=report, items=tuple(scored[: max(1, limit)]))


def free_text_retrieval(
    conn,
    data_dir: Path,
    *,
    query: str,
    limit: int = 20,
) -> SemanticRetrievalResult:
    """Match a natural-language query against stored enrichment claim text."""
    live = groups.live_stable_ids(conn)
    embeddings = _embeddings_for_live(data_dir, live)
    claims = _claims_for_live(conn, live)
    report = build_retrieval_report(
        "free_text",
        embedding_count=len(embeddings),
        claim_count=len(claims),
    )
    q = _tokens(query)
    if not q:
        return SemanticRetrievalResult(mode="free_text", report=report, items=())
    scored: list[SemanticCandidate] = []
    for sid in live:
        track_claims = claims.get(sid, [])
        text_factor = _text_factor(track_claims, query)
        if text_factor.score <= 0:
            continue
        emb_factor: RetrievalFactor | None = None
        vec = embeddings.get(sid)
        if vec is not None and len(embeddings) >= 2:
            # Baseline semantic leg: mean pool of live embeddings as a library centroid proxy.
            others = [v for other_sid, v in embeddings.items() if other_sid != sid]
            if others:
                dim = len(vec)
                mean = [sum(row[d] for row in others) / len(others) for d in range(dim)]
                sim = _cosine(vec, mean)
                emb_factor = RetrievalFactor(
                    kind="embedding",
                    score=round(sim, 4),
                    detail="cosine vs library centroid (local baseline)",
                )
        factors = [text_factor] + ([emb_factor] if emb_factor is not None else [])
        scored.append(
            SemanticCandidate(
                stable_id=sid,
                ranking_kind=CANDIDATE_RANKING,
                factors=tuple(factors),
            )
        )
    scored.sort(key=_rank_key)
    return SemanticRetrievalResult(mode="free_text", report=report, items=tuple(scored[: max(1, limit)]))


def hybrid_retrieval(
    conn,
    data_dir: Path,
    *,
    query: str,
    tag_filters: dict[str, str],
    limit: int = 20,
    filters: SemanticFilters | None = None,
    reference_stable_id: str | None = None,
) -> SemanticRetrievalResult:
    """Combine tag filters, claim text, and optional reference embedding factors."""
    live = groups.live_stable_ids(conn)
    embeddings = _embeddings_for_live(data_dir, live)
    claims = _claims_for_live(conn, live)
    report = build_retrieval_report(
        "hybrid",
        embedding_count=len(embeddings),
        claim_count=len(claims),
    )
    filt = filters or SemanticFilters()
    ref_vec = embeddings.get(reference_stable_id) if reference_stable_id else None
    scored: list[SemanticCandidate] = []
    for sid in live:
        track_claims = claims.get(sid, [])
        filter_factor = _filter_factor(conn, sid, filters=filt, tag_filters=tag_filters, claims=claims)
        if filter_factor is None:
            continue
        tag_factor = _tag_factor(track_claims, tag_filters)
        text_factor = _text_factor(track_claims, query) if query.strip() else None
        factors: list[RetrievalFactor] = [filter_factor, tag_factor]
        if text_factor is not None:
            factors.append(text_factor)
        vec = embeddings.get(sid)
        if ref_vec is not None and vec is not None:
            distance = round(1.0 - _cosine(ref_vec, vec), 6)
            factors.append(
                RetrievalFactor(
                    kind="embedding",
                    score=round(1.0 - distance, 4),
                    detail="cosine distance to reference embedding",
                )
            )
        elif query.strip() and vec is not None:
            factors.append(
                RetrievalFactor(
                    kind="embedding",
                    score=round(max(0.0, _text_factor(track_claims, query).score), 4),
                    detail="claim-text embedding proxy (no reference track)",
                )
            )
        if len(factors) < 2:
            continue
        scored.append(
            SemanticCandidate(
                stable_id=sid,
                ranking_kind=CANDIDATE_RANKING,
                factors=tuple(factors),
            )
        )
    scored.sort(key=_rank_key)
    return SemanticRetrievalResult(mode="hybrid", report=report, items=tuple(scored[: max(1, limit)]))


def parse_tag_filters(raw: str | None) -> dict[str, str]:
    """Parse ``dimension:value`` pairs comma-separated from a query string."""
    if not raw or not raw.strip():
        return {}
    out: dict[str, str] = {}
    for part in raw.split(","):
        piece = part.strip()
        if not piece:
            continue
        if ":" not in piece:
            raise ValueError(f"tag filter {piece!r} must be dimension:value")
        dim, val = piece.split(":", 1)
        dim, val = dim.strip(), val.strip()
        if not dim or not val:
            raise ValueError(f"tag filter {piece!r} must be dimension:value")
        out[dim] = val
    return out


def candidate_to_dict(candidate: SemanticCandidate) -> dict[str, Any]:
    return {
        "stable_id": candidate.stable_id,
        "ranking_kind": candidate.ranking_kind,
        "distance": candidate.distance,
        "factors": [{"kind": f.kind, "score": f.score, "detail": f.detail} for f in candidate.factors],
    }


def result_to_dict(result: SemanticRetrievalResult) -> dict[str, Any]:
    kinds = {f.kind for item in result.items for f in item.factors}
    return {
        "mode": result.mode,
        "ranking_kind": CANDIDATE_RANKING,
        "mix_compatible": False,
        "report": result.report.as_dict(),
        "factor_kinds_present": sorted(kinds),
        "items": [candidate_to_dict(item) for item in result.items],
    }


__all__ = [
    "CANDIDATE_RANKING",
    "LOCAL_BASELINE",
    "SIMILARITY_QUALITY_MIN_TRACKS",
    "SemanticCandidate",
    "SemanticFilters",
    "SemanticRetrievalResult",
    "RetrievalFactor",
    "RetrievalReport",
    "audio_reference_retrieval",
    "build_retrieval_report",
    "candidate_to_dict",
    "free_text_retrieval",
    "hybrid_retrieval",
    "parse_tag_filters",
    "result_to_dict",
]
