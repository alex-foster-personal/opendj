"""Tag values and tag types (Library Organization PRD R-TAG, R-TT).

Tag values stay in the ``tags`` row of ``track_fields``, the My Tag source of
truth. Writes run through :mod:`apps.shared.state.library_org`'s journaled
operation, so every assign, rename, merge and delete is one undoable step.
Tag identity is the normalised name, so spellings of one tag never split it.
"""
from __future__ import annotations

import sqlite3
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from .library_org import (
    GENERAL_TYPE,
    TAGS_FIELD,
    TRACK_FIELDS,
    OrgError,
    WriteResult,
    _live,
    _operation,
    _read,
    _read_journaled,
    _tag_list,
    _Unit,
    list_rows,
    normalize_tag_name,
)
from .writer import StateWriter

#: One write's track limit: a whole library selection, not an unbounded body.
MAX_TAG_TRACKS: int = 5000


#: Built-in meanings the suggester knows before anyone teaches it (R-TAG-2).
#: Read beside ``tag_synonym``, so seeding needs no synced rows.
SEED_SYNONYMS: dict[str, tuple[str, ...]] = {
    "sad": ("dark", "melancholic", "emotional"),
    "dark": ("sad", "moody", "melancholic"),
    "happy": ("euphoric", "uplifting", "joyful"),
    "euphoric": ("uplifting", "happy"),
    "uplifting": ("euphoric", "happy"),
    "angry": ("aggressive", "hard"),
    "aggressive": ("angry", "hard"),
    "chill": ("calm", "mellow", "relaxed"),
    "calm": ("chill", "mellow"),
    "strings": ("violin", "cello", "viola"),
    "violin": ("strings", "fiddle"),
    "fiddle": ("violin",),
    "cello": ("strings",),
    "keys": ("piano", "synth"),
    "piano": ("keys",),
    "vocals": ("vocal", "choir"),
    "singing": ("vocal", "choir"),
    "choir": ("vocal", "chorus"),
    "brass": ("trumpet", "horn", "trombone"),
    "drums": ("percussion",),
    "percussion": ("drums",),
    "peak": ("banger", "peak-time"),
    "banger": ("peak", "peak-time"),
    "warmup": ("warm-up", "opener"),
    "opener": ("warm-up", "warmup"),
    "closer": ("outro", "ending"),
}


@dataclass(frozen=True)
class TagSummary:
    """One tag as the library sees it. No ``tag_meta`` row means general (R-TT-1)."""

    name: str
    name_norm: str
    type: str
    color: str | None
    track_count: int


def _norm(name: str) -> str:
    """Normalise a name that may be stored or typed blank, without refusing it."""
    return unicodedata.normalize("NFKC", name).casefold().strip()


def _singular(norm: str) -> str:
    return norm[:-1] if len(norm) > 3 and norm.endswith("s") and not norm.endswith("ss") else norm


def track_tag_map(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Every live track that has tags, with its tag list."""
    rows = conn.execute(
        "SELECT f.stable_id, f.value_json FROM track_fields f "
        "JOIN tracks t ON t.stable_id = f.stable_id "
        "WHERE f.field_name = ? AND t.deleted_at IS NULL",
        (TAGS_FIELD,),
    ).fetchall()
    tags_by_track = {row[0]: _tag_list(row[1]) for row in rows}
    return {stable_id: tags for stable_id, tags in tags_by_track.items() if tags}


def tag_catalog(conn: sqlite3.Connection) -> list[TagSummary]:
    """Every tag in use or declared, one entry per normalised name."""
    counts: dict[str, int] = {}
    spellings: dict[str, dict[str, int]] = {}
    for tags in track_tag_map(conn).values():
        for norm in {_norm(tag) for tag in tags} - {""}:
            counts[norm] = counts.get(norm, 0) + 1
        for tag in tags:
            by_spelling = spellings.setdefault(_norm(tag), {})
            by_spelling[tag] = by_spelling.get(tag, 0) + 1
    metas = {row["name_norm"]: row for row in list_rows(conn, "tag_meta")}
    types = {row["id"] for row in list_rows(conn, "tag_type")}
    summaries = []
    for norm in sorted(set(counts) | set(metas)):
        meta = metas.get(norm)
        common = spellings.get(norm, {})
        name = meta["display_name"] if meta else max(sorted(common), key=common.__getitem__)
        tag_type = meta["type"] if meta and meta["type"] in types else GENERAL_TYPE
        summaries.append(TagSummary(
            name=name, name_norm=norm, type=tag_type,
            color=meta["color"] if meta else None, track_count=counts.get(norm, 0),
        ))
    return summaries


def _catalog_by_norm(conn: sqlite3.Connection) -> dict[str, TagSummary]:
    return {tag.name_norm: tag for tag in tag_catalog(conn)}


def _taught_terms(conn: sqlite3.Connection) -> dict[str, set[str]]:
    terms = {term: set(tags) for term, tags in SEED_SYNONYMS.items()}
    for row in list_rows(conn, "tag_synonym"):
        terms.setdefault(row["term_norm"], set()).add(row["tag_name_norm"])
    return terms


_SIMILAR_RANK: int = 3


def _literal_rank(query: str, norm: str) -> int | None:
    """0 exact, 1 prefix, 2 substring either way. ``violins`` finds ``violin``."""
    if norm == query or _singular(norm) == _singular(query):
        return 0
    if norm.startswith(query):
        return 1
    return 2 if query in norm or (len(norm) >= 3 and norm in query) else None


def suggest_tags(
    conn: sqlite3.Connection,
    query: str,
    *,
    tag_type: str | None = None,
    track_ids: Iterable[str] = (),
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Existing tags for ``query``: literal hits first, then ``similar`` ones (D9).

    A tag every one of ``track_ids`` already holds is left out, and ``tag_type``
    limits the answer to that type (R-TT-3). Suggestions never apply themselves.
    """
    candidates = _candidates(conn, tag_type, track_ids)
    needle = _norm(query)
    if not needle:
        ranked = sorted(candidates, key=lambda tag: (-tag.track_count, tag.name_norm))
        return [_suggestion(tag, False) for tag in ranked[:limit]]
    terms = _taught_terms(conn)
    similar = terms.get(needle, set()) | terms.get(_singular(needle), set())
    hits = [hit for tag in candidates if (hit := _hit(needle, tag, similar)) is not None]
    return [_suggestion(hit[3], hit[0] == _SIMILAR_RANK) for hit in sorted(hits, key=lambda h: h[:3])[:limit]]


def _candidates(
    conn: sqlite3.Connection, tag_type: str | None, track_ids: Iterable[str]
) -> list[TagSummary]:
    catalog = tag_catalog(conn)
    if tag_type is not None:
        catalog = [tag for tag in catalog if tag.type == tag_type]
    held = [{_norm(tag) for tag in _track_tags(conn, stable_id)} for stable_id in track_ids]
    if not held:
        return catalog
    return [tag for tag in catalog if not all(tag.name_norm in tags for tags in held)]


def _hit(
    needle: str, tag: TagSummary, similar: set[str]
) -> tuple[int, int, str, TagSummary] | None:
    rank = _literal_rank(needle, tag.name_norm)
    if rank is None and tag.name_norm in similar:
        rank = _SIMILAR_RANK
    return None if rank is None else (rank, -tag.track_count, tag.name_norm, tag)


def _suggestion(tag: TagSummary, similar: bool) -> dict[str, Any]:
    return {**asdict(tag), "similar": similar}


def _track_tags(conn: sqlite3.Connection, stable_id: str) -> list[str]:
    alive = conn.execute(
        "SELECT 1 FROM tracks WHERE stable_id = ? AND deleted_at IS NULL", (stable_id,)
    ).fetchone()
    if alive is None:
        raise OrgError("NOT_FOUND", f"track {stable_id} does not exist")
    row = _read_journaled(conn, TRACK_FIELDS, {"stable_id": stable_id, "field_name": TAGS_FIELD})
    return [] if row is None else row["value"]


def _declare_tag(
    unit: _Unit, known: TagSummary | None, name: str, tag_type: str
) -> dict[str, Any]:
    """Write the ``tag_meta`` row of a tag that has none, or retype the one it has."""
    conn = unit.writer._conn
    _live(conn, "tag_type", {"id": tag_type})
    norm = normalize_tag_name(name)
    current = _read(conn, "tag_meta", {"name_norm": norm})
    if current is not None and current["deleted_at"] is None:
        return unit.put("tag_meta", {"name_norm": norm}, {"type": tag_type})
    return unit.put("tag_meta", {"name_norm": norm}, {
        "display_name": known.name if known else name.strip(),
        "type": tag_type, "color": None, "source_category": None,
    })


def create_tag(
    writer: StateWriter, session_id: str, name: str, tag_type: str = GENERAL_TYPE
) -> WriteResult:
    """Declare a tag. The same name twice is the same tag, never a second one (R-TAG-5)."""
    norm = normalize_tag_name(name)
    with _operation(writer, session_id, "tag.create") as unit:
        known = _catalog_by_norm(writer._conn).get(norm)
        if known is not None and known.type != tag_type:
            raise OrgError("CONFLICT", f"#{known.name} already exists as a {known.type} tag")
        row = _declare_tag(unit, known, name, tag_type)
    return WriteResult(row, unit.undo_id)


def set_tag_type(writer: StateWriter, session_id: str, name: str, tag_type: str) -> WriteResult:
    """Move a tag to another type. Its tracks keep it (E7)."""
    norm = normalize_tag_name(name)
    with _operation(writer, session_id, "tag.retype") as unit:
        known = _catalog_by_norm(writer._conn).get(norm)
        if known is None:
            raise OrgError("NOT_FOUND", f"tag {name} does not exist")
        row = _declare_tag(unit, known, name, tag_type)
    return WriteResult(row, unit.undo_id)


def _without(tags: list[str], norms: set[str]) -> list[str]:
    return [tag for tag in tags if _norm(tag) not in norms]


def _checked_tracks(
    stable_ids: list[str], add: Sequence[str], remove: Sequence[str]
) -> list[str]:
    wanted = list(dict.fromkeys(stable_ids))
    if not wanted:
        raise OrgError("VALIDATION", "at least one track is required")
    if len(wanted) > MAX_TAG_TRACKS:
        raise OrgError("VALIDATION", f"at most {MAX_TAG_TRACKS} tracks at once")
    if not add and not remove:
        raise OrgError("VALIDATION", "at least one of add or remove is required")
    return wanted


def _resolve_additions(
    unit: _Unit, names: Sequence[str], tag_type: str | None
) -> dict[str, str]:
    """Each added name as the tag it is. A new name given a type declares that type."""
    known = _catalog_by_norm(unit.writer._conn)
    added: dict[str, str] = {}
    for name in names:
        norm = normalize_tag_name(name)
        added[norm] = known[norm].name if norm in known else name.strip()
        if tag_type is not None and norm not in known:
            _declare_tag(unit, None, name, tag_type)
    return added


def assign_tags(
    writer: StateWriter,
    session_id: str,
    stable_ids: list[str],
    *,
    add: Sequence[str] = (),
    remove: Sequence[str] = (),
    tag_type: str | None = None,
) -> WriteResult:
    """Add and remove tags on tracks as one undoable step (R-TAG-5, R-TAG-6).

    A name matching an existing tag by normalised form lands as that tag. A new
    name given a ``tag_type`` is declared as that type (R-TT-3). The row reports
    each track's resulting tags.
    """
    wanted = _checked_tracks(stable_ids, add, remove)
    removed = {normalize_tag_name(name) for name in remove}
    with _operation(writer, session_id, "tag.assign") as unit:
        conn = writer._conn
        added = _resolve_additions(unit, add, tag_type)
        result: dict[str, list[str]] = {}
        for stable_id in wanted:
            tags = _without(_track_tags(conn, stable_id), removed)
            held = {_norm(tag) for tag in tags}
            tags += [display for norm, display in added.items() if norm not in held]
            unit.set_tags(stable_id, tags)
            result[stable_id] = tags
    return WriteResult({"tracks": result}, unit.undo_id)


def rename_tag(
    writer: StateWriter, session_id: str, name: str, new_name: str, *, merge: bool = False
) -> WriteResult:
    """Rename a tag on every track. Into an existing tag needs ``merge`` (R-TAG-5, E5)."""
    source = normalize_tag_name(name)
    target = normalize_tag_name(new_name)
    with _operation(writer, session_id, "tag.rename") as unit:
        conn = writer._conn
        catalog = _catalog_by_norm(conn)
        if source not in catalog:
            raise OrgError("NOT_FOUND", f"tag {name} does not exist")
        existing = catalog.get(target) if target != source else None
        if existing is not None and not merge:
            raise OrgError(
                "CONFLICT",
                f"#{existing.name} already exists on {existing.track_count} track(s); "
                "rename with merge to combine them",
            )
        display = existing.name if existing is not None else new_name.strip()
        updated = _sweep(unit, source, display)
        _move_meta(unit, source, target, display, keep_target=existing is not None)
    return WriteResult(
        {"name": display, "tracks_updated": updated, "merged": existing is not None},
        unit.undo_id,
    )


def _move_meta(
    unit: _Unit, source: str, target: str, display: str, *, keep_target: bool
) -> None:
    """Carry a tag's type and colour to its new name. A merge keeps the target's."""
    meta = _read(unit.writer._conn, "tag_meta", {"name_norm": source})
    if meta is None or meta["deleted_at"] is not None:
        return
    if target == source:
        unit.put("tag_meta", {"name_norm": source}, {"display_name": display})
        return
    unit.tombstone("tag_meta", {"name_norm": source})
    if not keep_target:
        unit.put("tag_meta", {"name_norm": target}, {
            "display_name": display, "type": meta["type"], "color": meta["color"],
            "source_category": meta["source_category"],
        })


def _sweep(unit: _Unit, source: str, display: str | None) -> int:
    """Replace or drop one tag on every track holding it. Returns how many changed."""
    updated = 0
    for stable_id, tags in track_tag_map(unit.writer._conn).items():
        if source not in {_norm(tag) for tag in tags}:
            continue
        kept: list[str] = []
        for tag in tags:
            replacement = (display if _norm(tag) == source else tag)
            if replacement is not None and _norm(replacement) not in {_norm(t) for t in kept}:
                kept.append(replacement)
        unit.set_tags(stable_id, kept)
        updated += 1
    return updated


def delete_tag(writer: StateWriter, session_id: str, name: str) -> WriteResult:
    """Remove a tag from every track and drop its metadata, undoably."""
    source = normalize_tag_name(name)
    with _operation(writer, session_id, "tag.delete") as unit:
        conn = writer._conn
        if source not in _catalog_by_norm(conn):
            raise OrgError("NOT_FOUND", f"tag {name} does not exist")
        updated = _sweep(unit, source, None)
        meta = _read(conn, "tag_meta", {"name_norm": source})
        if meta is not None and meta["deleted_at"] is None:
            unit.tombstone("tag_meta", {"name_norm": source})
    return WriteResult({"tracks_updated": updated}, unit.undo_id)
