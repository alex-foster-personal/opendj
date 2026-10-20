"""The genre operations every surface shares: HTTP routes, the CLI and the MCP tools.

Each function takes an open state.db connection, so the three surfaces cannot
drift: ``GET /api/v1/genres``, ``python -m apps.genre_infer findings`` and the
MCP ``genres_get`` tool all return :func:`genres_for`'s document.

-Claude
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from apps.adapters.rekordbox import config as rb_config
from apps.shared.state import db as state_db
from apps.shared.state.locations import bulk_local_audio_paths

from . import findings, groups, library_tags, pipeline, providers, tag_write, taxonomy

MAX_IDS = 500


class UnknownTrack(KeyError):
    """A stable_id that is not a live track (HTTP 404)."""


def _require_tracks(conn: sqlite3.Connection, stable_ids: Sequence[str]) -> list[str]:
    ids = list(dict.fromkeys(stable_ids))
    if not ids or len(ids) > MAX_IDS:
        raise findings.GenreInputError(f"give 1 to {MAX_IDS} stable_ids, got {len(ids)}")
    live: set[str] = set()
    for start in range(0, len(ids), MAX_IDS):
        chunk = ids[start : start + MAX_IDS]
        marks = ",".join("?" * len(chunk))
        live.update(
            r[0]
            for r in conn.execute(
                f"SELECT stable_id FROM tracks WHERE deleted_at IS NULL AND stable_id IN ({marks})", chunk
            )
        )
    missing = [sid for sid in ids if sid not in live]
    if missing:
        raise UnknownTrack(f"not live tracks: {missing[:5]}")
    return ids


def genres_for(
    conn: sqlite3.Connection, stable_ids: Sequence[str], *, master_path: Path | None = None
) -> dict[str, Any]:
    """Every provider's finding plus the chosen genre, per track.

    GENRE-08: the findings are the track's duplicate GROUP's, one per provider
    (:func:`groups.resolve`), so a copy that was never asked about shows what
    its duplicate got; each finding names the copy that holds it
    (``stable_id``) and ``duplicates`` lists the other copies. ``user`` is the
    genre typed on any copy (newest edit); ``library-tag`` stays each row's own.

    ``library-tag`` is the rekordbox genre (``master_path``, default the
    configured master database), then the imported file tag, exactly as the
    Library row resolves it, so every surface and the file-tag writer choose
    the same genre the row shows.
    """
    ids = _require_tracks(conn, stable_ids)
    cfg = findings.read_config(conn)
    grp = groups.load(conn)
    stored = groups.findings_for(conn, grp, ids) if findings.has_schema(conn) else {}
    vector_of = groups.vector_holders(conn, stored) if findings.has_schema(conn) else {}
    typed = groups.user_genres(conn, grp, ids)
    fields = library_tags.field_genres(conn, ids)
    rekordbox = library_tags.rekordbox_genres(conn, ids, master_path or rb_config.MASTER_PLAIN_DB)
    tracks: dict[str, Any] = {}
    for sid in ids:
        own_typed, imported = fields.get(sid, (None, None))
        user = typed.get(sid) or own_typed
        library = rekordbox.get(sid) or imported
        chosen = findings.choose(stored.get(sid, ()), cfg.precedence, user_genre=user, library_genre=library)
        vector = vector_of.get(sid)
        tracks[sid] = {
            "chosen": None
            if chosen is None
            else {"genre": chosen.genre, "provider": chosen.provider, "confidence": chosen.confidence},
            "virtual": {findings.PROVIDER_USER: user, findings.PROVIDER_LIBRARY_TAG: library},
            "findings": [f.as_dict() for f in stored.get(sid, ())],
            "duplicates": list(grp.siblings_of(sid)),
            "vector": None if vector is None else {"provider": findings.PROVIDER_JEV, "stable_id": vector},
        }
    return {"precedence": list(cfg.precedence), "taxonomy_version": taxonomy.TAXONOMY_VERSION, "tracks": tracks}


def attach_finding(
    conn: sqlite3.Connection,
    *,
    stable_id: str,
    provider: str,
    genre: str,
    confidence: float,
    source: str,
    note: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Store an externally supplied finding (a person's or an agent's). Commits.

    GENRE-08: it applies to every duplicate of the track; another copy's finding
    from the same provider is replaced, so the newest attach is the one read.
    """
    findings.require_provider_id(provider, external=True)
    if not source.strip():
        raise findings.GenreInputError("source is required: say where the genre came from (a URL or a name)")
    findings.ensure_schema(conn)
    # One write transaction from the group read to the commit: the identity recorded,
    # the row stored and the copies cleared all see the same group.
    began = groups.begin_write(conn)
    try:
        return _attach(conn, stable_id, provider, genre, confidence, source, note, model)
    except BaseException:
        if began:
            conn.rollback()
        raise


def _attach(
    conn: sqlite3.Connection,
    stable_id: str,
    provider: str,
    genre: str,
    confidence: float,
    source: str,
    note: str | None,
    model: str | None,
) -> dict[str, Any]:
    _require_tracks(conn, [stable_id])
    grp = groups.load(conn)
    groups.bind_legacy(conn, grp)
    canonical = taxonomy.canonical(genre)
    stored = findings.put_finding(
        conn,
        findings.Finding(
            stable_id=stable_id,
            provider=provider,
            status=findings.STATUS_OK,
            genre=canonical or genre.strip(),
            confidence=confidence,
            evidence={
                "source": source.strip(),
                **({"note": note} if note else {}),
                # The group the finding is for: a later retag of this track makes it unshared.
                groups.EVIDENCE_IDENTITY: grp.key(stable_id),
            },
            model=model,
            version="external",
        ),
    )
    groups.delete_findings(conn, grp.siblings_of(stable_id), [provider])
    conn.commit()
    return stored.as_dict()


def remove_finding(conn: sqlite3.Connection, *, stable_id: str, provider: str) -> bool:
    findings.require_provider_id(provider, external=True)
    if not findings.has_schema(conn):
        return False
    # GENRE-08: the finding may sit on any copy; removing it removes it for the group
    # as it is now (the write lock is held from the group read to the commit).
    groups.begin_write(conn)
    removed = groups.delete_findings(conn, groups.load(conn).members_of(stable_id), [provider]) > 0
    conn.commit()
    return removed


def status(conn: sqlite3.Connection, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Whether inference can run, and how many findings exist per provider."""
    missing = providers.missing_keys(environ)
    counts: dict[str, int] = {}
    if findings.has_schema(conn):
        counts = {
            p: n
            for p, n in conn.execute(
                "SELECT provider, COUNT(*) FROM genre_findings WHERE status = 'ok' GROUP BY provider"
            )
        }
    return {
        "inference": {
            "available": not missing,
            "missing_keys": missing,
            "reason": None if not missing else f"{', '.join(missing)} not set",
            "search_query_version": providers.QUERY_VERSION,
            "models": {"jev": providers.JEV_MODEL, "deepseek": providers.DEEPSEEK_MODEL},
            "taxonomy_version": taxonomy.TAXONOMY_VERSION,
            "genres": list(taxonomy.NAMES),
        },
        "config": findings.read_config(conn).as_dict(),
        "findings_by_provider": counts,
    }


def settings_items(state_db_path: Path, environ: Mapping[str, str] | None = None) -> list[tuple[str, Any, str]]:
    """(key, value, note) rows for the read-only Settings page's "Genre inference" group."""
    missing = providers.missing_keys(environ)
    cfg = findings.GenreConfig()
    if state_db_path.is_file():
        conn = state_db.open_ro(state_db_path)
        try:
            cfg = findings.read_config(conn)
        finally:
            conn.close()
    note = "keys set in the engine's environment" if not missing else f"not available: set {', '.join(missing)}"
    return [
        ("genre_inference_available", not missing, note),
        ("genre_auto_infer", cfg.auto_infer, "infer genres for new tracks as they arrive (PUT /api/v1/genres/config)"),
        ("genre_precedence", " > ".join(cfg.precedence), "the first source with a genre is the one the Library shows"),
        ("genre_write_file_tags", cfg.write_file_tags, f"write roots: {', '.join(cfg.write_roots) or 'none'}"),
    ]


def infer(
    conn: sqlite3.Connection, stable_ids: Sequence[str], *, force: bool, environ: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Run inference now (synchronously) for up to :data:`MAX_IDS` tracks."""
    ids = _require_tracks(conn, stable_ids)
    keys = pipeline.required_keys(environ)
    return pipeline.infer_tracks(conn, ids, force=force, keys=keys).as_dict()


def similar(conn: sqlite3.Connection, stable_id: str, limit: int) -> dict[str, Any]:
    _require_tracks(conn, [stable_id])
    if not findings.has_schema(conn):
        raise KeyError(f"track {stable_id} has no genre vector; run genre inference first")
    neighbors = groups.similar_tracks(conn, groups.load(conn), stable_id, limit=limit)
    return {
        "stable_id": stable_id,
        "provider": findings.PROVIDER_JEV,
        "similar": [{"stable_id": s, "score": v} for s, v in neighbors],
    }


def write_chosen_tag(
    conn: sqlite3.Connection, *, data_dir: Path, stable_id: str, master_path: Path | None = None
) -> dict[str, Any]:
    """Write the chosen genre into the track's audio file (GENRE-05). Commits."""
    doc = genres_for(conn, [stable_id], master_path=master_path)["tracks"][stable_id]
    chosen = doc["chosen"]
    if chosen is None:
        raise tag_write.TagWriteRefused(f"track {stable_id} has no chosen genre to write")
    if chosen["provider"] == findings.PROVIDER_LIBRARY_TAG:
        raise tag_write.TagWriteRefused("the chosen genre is already the library's own tag; nothing to write")
    path = bulk_local_audio_paths(conn, [stable_id]).get(stable_id)
    if path is None:
        raise tag_write.TagWriteRefused(f"track {stable_id} has no audio file on this machine")
    result = tag_write.write_genre(
        conn,
        data_dir=data_dir,
        stable_id=stable_id,
        path=Path(path),
        genre=chosen["genre"],
        provider=chosen["provider"],
    )
    return result.__dict__


__all__ = [
    "MAX_IDS",
    "UnknownTrack",
    "attach_finding",
    "genres_for",
    "infer",
    "remove_finding",
    "settings_items",
    "similar",
    "status",
    "write_chosen_tag",
]
