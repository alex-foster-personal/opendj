"""Per-track, per-provider genre findings in state.db, and the one genre the library shows.

Tables (machine-local, created on first use like the analysis queue's tables):

* ``genre_findings``: one row per (track, provider). ``odj-fused``, ``jev`` and
  ``deepseek`` are written by :mod:`apps.genre_infer.pipeline`; any other
  provider id (``beatport``, ``agent.discogs``, ...) is attached by a person or an
  agent through the HTTP, CLI or MCP surface, with the source it came from.
* ``genre_vectors``: JEV's full probability vector over the taxonomy per track,
  kept for genre similarity (:func:`similar_tracks`).
* ``genre_search_cache``: one web search result per frozen query string, so a
  search is never paid for twice.
* ``genre_config``: precedence, auto-inference and file-tag write settings.
* ``genre_tag_writes``: the undo journal of genre tags written into audio files.

The library shows ONE genre per track: the first provider in the configured
precedence that has an ``ok`` finding (:func:`choose`). ``user`` (a genre typed in
the library) and ``library-tag`` (the rekordbox or file tag) are virtual
providers read from the track row itself, never copied into this table.

-Claude
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import taxonomy

# ----------------------------------------------------------------- providers
PROVIDER_FUSED = "odj-fused"
PROVIDER_JEV = "jev"
PROVIDER_DEEPSEEK = "deepseek"
PROVIDER_USER = "user"
PROVIDER_LIBRARY_TAG = "library-tag"
COMPUTED_PROVIDERS: tuple[str, ...] = (PROVIDER_FUSED, PROVIDER_JEV, PROVIDER_DEEPSEEK)
VIRTUAL_PROVIDERS: tuple[str, ...] = (PROVIDER_USER, PROVIDER_LIBRARY_TAG)
_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,39}$")
MAX_GENRE_CHARS = 80
MAX_EVIDENCE_CHARS = 4000

STATUS_OK = "ok"
STATUS_UNKNOWN = "unknown"

# Trusted human or agent sources outrank our own guess; the library's existing
# rekordbox or file tag is the last resort (owner, Mon 12 Oct 2026: the library
# shows one genre in the Beatport taxonomy, which our guess supplies).
DEFAULT_PRECEDENCE: tuple[str, ...] = (PROVIDER_USER, "beatport", PROVIDER_FUSED, PROVIDER_LIBRARY_TAG)


class GenreInputError(ValueError):
    """A finding, provider id or config value that fails validation (HTTP 422)."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def require_provider_id(provider: str, *, external: bool) -> str:
    """A valid provider id; ``external`` refuses the ids the pipeline and the row own."""
    if not _PROVIDER_ID.match(provider):
        raise GenreInputError(f"provider {provider!r} must match {_PROVIDER_ID.pattern}")
    if external and provider in (*COMPUTED_PROVIDERS, *VIRTUAL_PROVIDERS):
        raise GenreInputError(f"provider {provider!r} is reserved for Open DJ; pick another id such as 'beatport'")
    return provider


# -------------------------------------------------------------------- schema
_SCHEMA: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS genre_findings (
        stable_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('ok', 'unknown')),
        genre TEXT,
        confidence REAL,
        reason TEXT,
        evidence_json TEXT NOT NULL DEFAULT '{}',
        model TEXT,
        version TEXT,
        request_digest TEXT,
        cost_usd REAL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (stable_id, provider)
    )""",
    "CREATE INDEX IF NOT EXISTS genre_findings_provider ON genre_findings(provider)",
    """CREATE TABLE IF NOT EXISTS genre_vectors (
        stable_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        taxonomy_version TEXT NOT NULL,
        vector_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (stable_id, provider)
    )""",
    """CREATE TABLE IF NOT EXISTS genre_search_cache (
        query TEXT PRIMARY KEY,
        query_version TEXT NOT NULL,
        text TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS genre_config (
        key TEXT PRIMARY KEY,
        value_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS genre_tag_writes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        stable_id TEXT NOT NULL,
        path TEXT NOT NULL,
        previous_genre TEXT,
        written_genre TEXT NOT NULL,
        provider TEXT NOT NULL,
        backup_path TEXT NOT NULL,
        written_at TEXT NOT NULL,
        undone_at TEXT,
        post_sha256 TEXT
    )""",
)
TABLES: tuple[str, ...] = ("genre_findings", "genre_vectors", "genre_search_cache", "genre_config", "genre_tag_writes")


def ensure_schema(conn: sqlite3.Connection) -> None:
    for stmt in _SCHEMA:
        conn.execute(stmt)


def has_schema(conn: sqlite3.Connection) -> bool:
    """True when the findings table exists (a read-only connection cannot create it)."""
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'genre_findings'").fetchone()
    return row is not None


# ------------------------------------------------------------------ findings
@dataclass(frozen=True)
class Finding:
    stable_id: str
    provider: str
    status: str
    genre: str | None
    confidence: float | None
    reason: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    model: str | None = None
    version: str | None = None
    request_digest: str | None = None
    cost_usd: float | None = None
    created_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["evidence"] = dict(self.evidence)
        out["in_taxonomy"] = self.genre is not None and taxonomy.canonical(self.genre) == self.genre
        return out


def _finding_from_row(row: Sequence[Any]) -> Finding:
    return Finding(
        stable_id=row[0],
        provider=row[1],
        status=row[2],
        genre=row[3],
        confidence=row[4],
        reason=row[5],
        evidence=json.loads(row[6] or "{}"),
        model=row[7],
        version=row[8],
        request_digest=row[9],
        cost_usd=row[10],
        created_at=row[11],
    )


_COLUMNS = "stable_id, provider, status, genre, confidence, reason, evidence_json, model, version, request_digest, cost_usd, created_at"


def put_finding(conn: sqlite3.Connection, f: Finding) -> Finding:
    """Insert or replace the (track, provider) finding; returns it as stored."""
    if f.status not in (STATUS_OK, STATUS_UNKNOWN):
        raise GenreInputError(f"status {f.status!r} must be 'ok' or 'unknown'")
    if f.status == STATUS_OK:
        if not f.genre or not f.genre.strip() or len(f.genre) > MAX_GENRE_CHARS:
            raise GenreInputError(f"an ok finding needs a genre of 1 to {MAX_GENRE_CHARS} characters")
        if f.confidence is None or not 0.0 <= float(f.confidence) <= 1.0 or math.isnan(float(f.confidence)):
            raise GenreInputError("an ok finding needs a confidence in [0, 1]")
    evidence_json = json.dumps(dict(f.evidence), ensure_ascii=False, sort_keys=True)
    if len(evidence_json) > MAX_EVIDENCE_CHARS:
        raise GenreInputError(f"evidence is {len(evidence_json)} characters; the limit is {MAX_EVIDENCE_CHARS}")
    stored = Finding(**{**asdict(f), "created_at": f.created_at or _now(), "evidence": dict(f.evidence)})
    conn.execute(
        f"INSERT OR REPLACE INTO genre_findings ({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            stored.stable_id,
            stored.provider,
            stored.status,
            stored.genre.strip() if stored.genre else None,
            stored.confidence,
            stored.reason,
            evidence_json,
            stored.model,
            stored.version,
            stored.request_digest,
            stored.cost_usd,
            stored.created_at,
        ),
    )
    return stored


def delete_finding(conn: sqlite3.Connection, stable_id: str, provider: str) -> bool:
    return (
        conn.execute("DELETE FROM genre_findings WHERE stable_id = ? AND provider = ?", (stable_id, provider)).rowcount
        > 0
    )


def findings_for(
    conn: sqlite3.Connection, stable_ids: Iterable[str] | None = None, *, providers: Sequence[str] | None = None
) -> dict[str, list[Finding]]:
    """stable_id -> its findings, newest first. ``None`` reads every track (one query).

    ``providers`` narrows the read to those provider ids.
    """
    out: dict[str, list[Finding]] = {}
    only = "" if providers is None else f" AND provider IN ({','.join('?' * len(providers))})"
    bound = [] if providers is None else list(providers)
    if stable_ids is None:
        rows: Iterable[Sequence[Any]] = conn.execute(
            f"SELECT {_COLUMNS} FROM genre_findings WHERE 1 = 1{only} ORDER BY created_at DESC", bound
        )
        for row in rows:
            out.setdefault(row[0], []).append(_finding_from_row(row))
        return out
    ids = list(dict.fromkeys(stable_ids))
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT {_COLUMNS} FROM genre_findings WHERE stable_id IN ({marks}){only} ORDER BY created_at DESC",
            [*chunk, *bound],
        ):
            out.setdefault(row[0], []).append(_finding_from_row(row))
    return out


# ------------------------------------------------------------------- choice
@dataclass(frozen=True)
class Chosen:
    genre: str
    provider: str
    confidence: float | None


def choose(
    findings: Iterable[Finding],
    precedence: Sequence[str],
    *,
    user_genre: str | None = None,
    library_genre: str | None = None,
) -> Chosen | None:
    """The first provider in ``precedence`` with an ``ok`` genre, else None.

    A provider missing from ``precedence`` is never shown: precedence is the
    whole display policy, with no hidden fallback.
    """
    candidates = {
        f.provider: Chosen(f.genre, f.provider, f.confidence) for f in findings if f.status == STATUS_OK and f.genre
    }
    for provider, genre, confidence in ((PROVIDER_USER, user_genre, 1.0), (PROVIDER_LIBRARY_TAG, library_genre, None)):
        if genre and genre.strip():
            candidates[provider] = Chosen(genre.strip(), provider, confidence)
    return next((candidates[p] for p in precedence if p in candidates), None)


# ------------------------------------------------------------------- vectors
def put_vector(conn: sqlite3.Connection, stable_id: str, provider: str, vector: Mapping[str, float]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO genre_vectors (stable_id, provider, taxonomy_version, vector_json, created_at) VALUES (?,?,?,?,?)",
        (stable_id, provider, taxonomy.TAXONOMY_VERSION, json.dumps(dict(vector), sort_keys=True), _now()),
    )


def vectors(conn: sqlite3.Connection, provider: str = PROVIDER_JEV) -> dict[str, dict[str, float]]:
    """stable_id -> vector of LIVE tracks, for the CURRENT taxonomy version only.

    Other taxonomy versions answer a different question; a deleted track is no
    neighbor anyone can use.
    """
    return {
        sid: json.loads(raw)
        for sid, raw in conn.execute(
            "SELECT v.stable_id, v.vector_json FROM genre_vectors v JOIN tracks t ON t.stable_id = v.stable_id "
            "WHERE v.provider = ? AND v.taxonomy_version = ? AND t.deleted_at IS NULL",
            (provider, taxonomy.TAXONOMY_VERSION),
        )
    }


def _unit(vec: Mapping[str, float]) -> list[float]:
    values = [float(vec.get(g, 0.0)) for g in taxonomy.NAMES]
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values] if norm > 0 else values


def similar_tracks(
    conn: sqlite3.Connection, stable_id: str, *, limit: int = 20, provider: str = PROVIDER_JEV
) -> list[tuple[str, float]]:
    """Tracks whose genre probability vector is closest (cosine) to ``stable_id``'s.

    Raises KeyError when the track has no vector, so "no neighbors" is never
    confused with "never measured".
    """
    all_vectors = vectors(conn, provider)
    if stable_id not in all_vectors:
        raise KeyError(f"track {stable_id} has no {provider} genre vector; run genre inference first")
    target = all_vectors.pop(stable_id)
    return nearest(all_vectors, target, limit)


def nearest(
    candidates: Mapping[str, Mapping[str, float]], target: Mapping[str, float], limit: int
) -> list[tuple[str, float]]:
    """The ``limit`` candidates closest (cosine) to ``target``, best first, ties by id."""
    unit = _unit(target)
    scored = [
        (sid, round(sum(a * b for a, b in zip(unit, _unit(vec), strict=True)), 4)) for sid, vec in candidates.items()
    ]
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored[: max(1, limit)]


# -------------------------------------------------------------- search cache
def cached_search(conn: sqlite3.Connection, query: str, query_version: str) -> str | None:
    """The cached result for ``query`` under ``query_version``; another version's row is stale."""
    row = conn.execute(
        "SELECT text FROM genre_search_cache WHERE query = ? AND query_version = ?", (query, query_version)
    ).fetchone()
    return None if row is None else str(row[0])


def put_search(conn: sqlite3.Connection, query: str, query_version: str, text: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO genre_search_cache (query, query_version, text, created_at) VALUES (?,?,?,?)",
        (query, query_version, text, _now()),
    )


def import_search_cache(conn: sqlite3.Connection, path: Path, query_version: str) -> int:
    """Load a ``{query: {"text": ...}}`` JSON cache (the calibration's) into the table; returns rows added."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise GenreInputError(f"{path} is not a JSON object of query -> result")
    added = 0
    for query, value in doc.items():
        text = value.get("text") if isinstance(value, dict) else None
        if (
            isinstance(query, str)
            and isinstance(text, str)
            and text.strip()
            and cached_search(conn, query, query_version) is None
        ):
            put_search(conn, query, query_version, text)
            added += 1
    return added


# -------------------------------------------------------------------- config
@dataclass(frozen=True)
class GenreConfig:
    precedence: tuple[str, ...] = DEFAULT_PRECEDENCE
    auto_infer: bool = False
    write_file_tags: bool = False
    write_roots: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "precedence": list(self.precedence),
            "auto_infer": self.auto_infer,
            "write_file_tags": self.write_file_tags,
            "write_roots": list(self.write_roots),
        }


_CONFIG_KEYS = ("precedence", "auto_infer", "write_file_tags", "write_roots")


def _validated_precedence(precedence: object) -> tuple[str, ...]:
    if (
        not isinstance(precedence, list)
        or not precedence
        or not all(isinstance(p, str) for p in precedence)
        or len(set(precedence)) != len(precedence)
    ):
        raise GenreInputError("precedence must be a non-empty list of distinct provider ids")
    for provider in precedence:
        require_provider_id(provider, external=False)
    return tuple(precedence)


def _validated_roots(roots: object) -> tuple[str, ...]:
    if not isinstance(roots, list) or not all(isinstance(r, str) and Path(r).is_absolute() for r in roots):
        raise GenreInputError("write_roots must be a list of absolute directory paths")
    return tuple(str(Path(r)) for r in roots)


def _validated_config(raw: Mapping[str, Any]) -> GenreConfig:
    unknown = sorted(set(raw) - set(_CONFIG_KEYS))
    if unknown:
        raise GenreInputError(f"unknown genre config keys {unknown}; known keys are {list(_CONFIG_KEYS)}")
    base = GenreConfig()
    for flag in ("auto_infer", "write_file_tags"):
        if flag in raw and not isinstance(raw[flag], bool):
            raise GenreInputError(f"{flag} must be true or false")
    return GenreConfig(
        precedence=_validated_precedence(raw.get("precedence", list(base.precedence))),
        auto_infer=bool(raw.get("auto_infer", base.auto_infer)),
        write_file_tags=bool(raw.get("write_file_tags", base.write_file_tags)),
        write_roots=_validated_roots(raw.get("write_roots", [])),
    )


def read_config(conn: sqlite3.Connection) -> GenreConfig:
    if not has_schema(conn):
        return GenreConfig()
    raw = {key: json.loads(value) for key, value in conn.execute("SELECT key, value_json FROM genre_config")}
    return _validated_config(raw)


def update_config(conn: sqlite3.Connection, patch: Mapping[str, Any]) -> GenreConfig:
    """Merge ``patch`` into the stored config after validating the result as a whole."""
    merged = {**read_config(conn).as_dict(), **dict(patch)}
    cfg = _validated_config(merged)
    for key, value in cfg.as_dict().items():
        conn.execute(
            "INSERT OR REPLACE INTO genre_config (key, value_json, updated_at) VALUES (?,?,?)",
            (key, json.dumps(value), _now()),
        )
    return cfg


__all__ = [
    "COMPUTED_PROVIDERS",
    "DEFAULT_PRECEDENCE",
    "PROVIDER_DEEPSEEK",
    "PROVIDER_FUSED",
    "PROVIDER_JEV",
    "PROVIDER_LIBRARY_TAG",
    "PROVIDER_USER",
    "STATUS_OK",
    "STATUS_UNKNOWN",
    "TABLES",
    "VIRTUAL_PROVIDERS",
    "Chosen",
    "Finding",
    "GenreConfig",
    "GenreInputError",
    "cached_search",
    "choose",
    "delete_finding",
    "ensure_schema",
    "findings_for",
    "has_schema",
    "import_search_cache",
    "nearest",
    "put_finding",
    "put_search",
    "put_vector",
    "read_config",
    "require_provider_id",
    "similar_tracks",
    "update_config",
    "vectors",
]
