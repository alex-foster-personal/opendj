"""Duplicates of a track share one genre result (GENRE-08).

A duplicate group is every live track with the same normalized title and the
same set of normalized artists (:func:`apps.shared.song_identity.title_artists_key`,
the enrich card's song key without its length part). Genre inference asks about
an artist and a title, so every copy and every edit of that work would get the
same question; it is asked once and the answer is read by every member.

Nothing is copied between rows. A finding stays on the copy it was stored for,
and every read resolves through the group (:func:`findings_for`,
:func:`vectors`, :func:`user_genres`), so a copy with no file on this machine
shows the same findings as the copy that was asked about.

When more than one member holds a finding of the same provider (rows written
before sharing existed), ONE wins, deterministically (:func:`resolve`): ``ok``
before ``unknown``, then the highest confidence, then the newest, then the
lowest stable_id. The provider precedence then picks the genre to show, exactly
as for a track with no duplicates.

A track with no title or no artist, or whose title or artist is a placeholder
(``ID``, ``Unknown``, ``Untitled``, ``Track 3``), is a group of one: two
unreleased ``ID - ID`` tracks are two different tracks.

-Claude
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from apps.shared.song_identity import artists_from_json, norm, title_artists_key

from . import findings, providers, taxonomy

# "ID - ID" and friends: an unreleased track. Two of them are two different
# tracks, so a placeholder never groups (and a search for it finds nothing useful).
_PLACEHOLDER = re.compile(r"^\s*(id|unknown|untitled|track\s*\d*)\s*$", re.IGNORECASE)
#: The evidence key under which a finding records the duplicate group it was made for.
EVIDENCE_IDENTITY = "identity"
_CHUNK = 500
# A read for more ids than this (the library index) is one unfiltered query, not id chunks.
_READ_ALL_OVER = 2000
# The trigger-maintained token of the ``tracks`` table (migration v21): it changes
# on every insert, update and delete, so it is an exact invalidation key.
_TOKEN_SQL = "SELECT token FROM sync_write_tokens WHERE table_name = 'tracks'"


@dataclass(frozen=True)
class Groups:
    """Every live track's duplicate group."""

    key_of: dict[str, str] = field(default_factory=dict)
    members: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: stable_id -> the normalized search query inference would send for the track now.
    asked_as: dict[str, str] = field(default_factory=dict)

    def key(self, stable_id: str) -> str:
        """The group key; a track that is not live is a group of one."""
        return self.key_of.get(stable_id, f"id:{stable_id}")

    def members_of(self, stable_id: str) -> tuple[str, ...]:
        """Every member of ``stable_id``'s group, itself included, sorted."""
        return self.members.get(self.key(stable_id), (stable_id,))

    def siblings_of(self, stable_id: str) -> tuple[str, ...]:
        return tuple(m for m in self.members_of(stable_id) if m != stable_id)

    def first_per_group(self, stable_ids: Iterable[str]) -> list[str]:
        """``stable_ids`` with every later member of an already seen group dropped."""
        seen: set[str] = set()
        out: list[str] = []
        for sid in stable_ids:
            key = self.key(sid)
            if key not in seen:
                seen.add(key)
                out.append(sid)
        return out


def nameable(artist: str, title: str) -> bool:
    """True when the artist and the title name a track: neither empty nor a placeholder."""
    return bool(artist and title) and not _PLACEHOLDER.match(title) and not _PLACEHOLDER.match(artist)


def _group_key(sid: str, title: str | None, artists: Sequence[str]) -> str:
    # Placeholders are judged on the text the key is built from (normalized, each artist once),
    # so a full-width ``ID`` or an ``ID`` listed twice is still a placeholder.
    named = ", ".join(sorted({norm(a) for a in artists if norm(a)}))
    key = title_artists_key(title, artists) if nameable(named, norm(title or "")) else None
    return key or f"id:{sid}"


def _query_text(artists: Sequence[str], title: str | None) -> str:
    """The normalized search query inference sends for these tags (artists as the Library shows them)."""
    shown = ", ".join(name for name in (a.strip() for a in artists) if name)
    return norm(providers.search_query(shown, (title or "").strip()))


def build(rows: Iterable[tuple[str, str | None, str | None]]) -> Groups:
    """Groups for ``(stable_id, title, artists_json)`` rows of live tracks."""
    key_of: dict[str, str] = {}
    members: dict[str, list[str]] = {}
    asked_as: dict[str, str] = {}
    for sid, title, artists_json in rows:
        artists = artists_from_json(artists_json)
        key = _group_key(str(sid), title, artists)
        key_of[str(sid)] = key
        members.setdefault(key, []).append(str(sid))
        asked_as[str(sid)] = _query_text(artists, title)
    return Groups(key_of, {key: tuple(sorted(ids)) for key, ids in members.items()}, asked_as)


_cache_lock = threading.Lock()
_cache: dict[str, tuple[bytes, Groups]] = {}


def _tracks_token(conn: sqlite3.Connection) -> tuple[str, bytes] | None:
    """(database file, tracks write token), or None when there is no file or no token to key on."""
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None or not row[2]:
        return None
    has_tokens = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sync_write_tokens'"
    ).fetchone()
    token = conn.execute(_TOKEN_SQL).fetchone() if has_tokens else None
    if token is None or token[0] is None:
        return None
    return str(Path(row[2]).resolve()), bytes(token[0])


def load(conn: sqlite3.Connection) -> Groups:
    """The groups of every live track in ``conn``'s library.

    One read of ``tracks``. Kept per database file for as long as the ``tracks``
    write token is unchanged; a database without that token (an in-memory one, or
    one older than migration v21) is read every time.
    """
    keyed = _tracks_token(conn)
    if keyed is not None:
        # Hold the lock across miss + build + store. A plain check-then-act outside
        # the lock let two concurrent loaders both build, store different Group
        # graphs (stable_id iteration order), and race the cache -- downstream
        # readers (e.g. FTS index rebuild) then saw nondeterministic snippets.
        with _cache_lock:
            hit = _cache.get(keyed[0])
            if hit is not None and hit[0] == keyed[1]:
                return hit[1]
            groups = build(
                conn.execute("SELECT stable_id, title, artists_json FROM tracks WHERE deleted_at IS NULL")
            )
            _cache[keyed[0]] = (keyed[1], groups)
            return groups
    return build(conn.execute("SELECT stable_id, title, artists_json FROM tracks WHERE deleted_at IS NULL"))


def begin_write(conn: sqlite3.Connection) -> bool:
    """Take the database's write lock now and hold it to the caller's commit; True when this call began it.

    A finding is stored against the group read at that moment; with the lock held
    from before that read, no import or sync can retag a track between the read
    and the write. A connection already in a transaction is left as it is (False):
    its transaction is the caller's to commit or roll back.
    """
    if conn.in_transaction:
        return False
    conn.execute("BEGIN IMMEDIATE")
    return True


# ------------------------------------------------------------------ findings
def _rank(f: findings.Finding) -> tuple[int, float, str]:
    """Higher wins: ok before unknown, then confidence, then the newer row."""
    return (1 if f.status == findings.STATUS_OK else 0, float(f.confidence or 0.0), f.created_at or "")


def resolve(rows: Iterable[findings.Finding]) -> list[findings.Finding]:
    """One finding per provider out of a group's members' findings, newest first."""
    best: dict[str, findings.Finding] = {}
    for f in sorted(rows, key=lambda f: f.stable_id):
        held = best.get(f.provider)
        if held is None or _rank(f) > _rank(held):
            best[f.provider] = f
    return _newest_first(best.values())


def _member_ids(groups: Groups, stable_ids: Sequence[str]) -> list[str] | None:
    """Every member of ``stable_ids``' groups; None (read everything) past :data:`_READ_ALL_OVER` ids."""
    if len(stable_ids) > _READ_ALL_OVER:
        return None
    return list(dict.fromkeys(m for sid in stable_ids for m in groups.members_of(sid)))


def describes_holder(f: findings.Finding, groups: Groups) -> bool:
    """False when the finding was made for another title or artists than its holder has now.

    A finding is about the track as it was named when the finding was made. If the
    holder was retagged since, the finding is not shared with the copies of its new
    name and does not count as that group's result. Findings written here record
    the group they were made for (``evidence.identity``); a computed finding from
    before that is checked against the search query it stored, as text, until
    :func:`bind_legacy` records its identity; a finding that
    recorded neither (attached from outside before this) cannot be checked and is
    taken as still describing its holder.
    """
    identity = f.evidence.get(EVIDENCE_IDENTITY)
    if identity is not None:
        return bool(identity == groups.key(f.stable_id))
    query = f.evidence.get("search_query")
    if isinstance(query, str):
        return norm(query) == groups.asked_as.get(f.stable_id)
    return True


def bind_legacy(conn: sqlite3.Connection, groups: Groups) -> int:
    """Record the identity of computed findings stored before identities were; returns how many.

    Only a finding whose stored search query is exactly the query its holder would
    be asked with now is bound: that is the one moment its group is known for
    certain. From then on it is judged by its group (so the same artists listed
    the other way round change nothing), not by the query text. A finding whose
    holder was already retagged stays unbound and is read by its holder alone.
    Needs a writable connection; the caller commits.
    """
    unbound = [
        (str(sid), str(provider), evidence_json)
        for sid, provider, evidence_json in conn.execute(
            "SELECT stable_id, provider, evidence_json FROM genre_findings "
            "WHERE json_extract(evidence_json, '$.search_query') IS NOT NULL "
            f"AND json_extract(evidence_json, '$.{EVIDENCE_IDENTITY}') IS NULL"
        )
    ]
    bound = 0
    for sid, provider, evidence_json in unbound:
        beside = findings.Finding(sid, provider, findings.STATUS_OK, None, None, evidence=json.loads(evidence_json))
        if sid in groups.key_of and describes_holder(beside, groups):
            conn.execute(
                f"UPDATE genre_findings SET evidence_json = json_set(evidence_json, '$.{EVIDENCE_IDENTITY}', ?) "
                "WHERE stable_id = ? AND provider = ?",
                (groups.key(sid), sid, provider),
            )
            bound += 1
    return bound


def _newest_first(rows: Iterable[findings.Finding]) -> list[findings.Finding]:
    return sorted(rows, key=lambda f: (f.created_at or "", f.provider), reverse=True)


def findings_for(
    conn: sqlite3.Connection, groups: Groups, stable_ids: Sequence[str], *, providers: Sequence[str] | None = None
) -> dict[str, list[findings.Finding]]:
    """stable_id -> its GROUP's findings, one per provider (:func:`resolve`).

    Each finding keeps the ``stable_id`` of the copy that holds it. ``providers``
    narrows the read to those provider ids (the library listing reads only the
    ones in the precedence). A finding that no longer describes its holder
    (:func:`describes_holder`) is read by its holder alone, and only for a
    provider the group has no finding of.
    """
    wanted = {groups.key(sid) for sid in stable_ids}
    stored = findings.findings_for(conn, _member_ids(groups, stable_ids), providers=providers)
    shared, own = _by_reader(groups, {sid: rows for sid, rows in stored.items() if groups.key(sid) in wanted})
    resolved = {key: resolve(rows) for key, rows in shared.items()}
    out: dict[str, list[findings.Finding]] = {}
    for sid in stable_ids:
        rows = resolved.get(groups.key(sid), [])
        held = {f.provider for f in rows}
        mine = [f for f in resolve(own.get(sid, ())) if f.provider not in held]
        if rows or mine:
            out[sid] = _newest_first([*rows, *mine]) if mine else rows
    return out


def _by_reader(
    groups: Groups, stored: dict[str, list[findings.Finding]]
) -> tuple[dict[str, list[findings.Finding]], dict[str, list[findings.Finding]]]:
    """(group key -> findings every member reads, stable_id -> findings only that holder reads)."""
    shared: dict[str, list[findings.Finding]] = {}
    own: dict[str, list[findings.Finding]] = {}
    for sid, rows in stored.items():
        for f in rows:
            if describes_holder(f, groups):
                shared.setdefault(groups.key(sid), []).append(f)
            else:
                own.setdefault(sid, []).append(f)
    return shared, own


def delete_findings(conn: sqlite3.Connection, stable_ids: Sequence[str], provider_ids: Sequence[str]) -> int:
    """Drop ``stable_ids``' findings and vectors of ``provider_ids``; returns the findings removed.

    Called with a group's OTHER members when one member's finding is stored, so
    the group keeps one finding per provider and the newest write is the one read.
    """
    removed = 0
    for start in range(0, len(stable_ids), _CHUNK):
        chunk = list(stable_ids[start : start + _CHUNK])
        where = f"provider IN ({','.join('?' * len(provider_ids))}) AND stable_id IN ({','.join('?' * len(chunk))})"
        removed += conn.execute(f"DELETE FROM genre_findings WHERE {where}", [*provider_ids, *chunk]).rowcount
        conn.execute(f"DELETE FROM genre_vectors WHERE {where}", [*provider_ids, *chunk])
    return removed


# ------------------------------------------------------------------- vectors
def vectors(
    conn: sqlite3.Connection, groups: Groups, provider: str = findings.PROVIDER_JEV
) -> dict[str, tuple[str, dict[str, float]]]:
    """group key -> (holder stable_id, vector) for the CURRENT taxonomy version.

    One vector per group: the one beside the provider finding :func:`resolve`
    picks (ok, then confidence, then newest, then the lowest stable_id). A vector
    whose finding no longer describes its holder is its holder's alone, under
    :func:`_own_key`.
    """
    out: dict[str, tuple[str, dict[str, float]]] = {}
    for sid, raw, evidence_json in conn.execute(
        "SELECT v.stable_id, v.vector_json, f.evidence_json FROM genre_vectors v "
        "JOIN tracks t ON t.stable_id = v.stable_id "
        "LEFT JOIN genre_findings f ON f.stable_id = v.stable_id AND f.provider = v.provider "
        "WHERE v.provider = ? AND v.taxonomy_version = ? AND t.deleted_at IS NULL "
        "ORDER BY (f.status = 'ok') DESC, f.confidence DESC, f.created_at DESC, v.stable_id",
        (provider, taxonomy.TAXONOMY_VERSION),
    ):
        beside = findings.Finding(
            str(sid), provider, findings.STATUS_OK, None, None, evidence=json.loads(evidence_json or "{}")
        )
        key = groups.key(str(sid)) if describes_holder(beside, groups) else _own_key(str(sid))
        out.setdefault(key, (str(sid), json.loads(raw)))
    return out


def _own_key(stable_id: str) -> str:
    return f"own:{stable_id}"


def vector_holders(
    conn: sqlite3.Connection, resolved: dict[str, list[findings.Finding]], provider: str = findings.PROVIDER_JEV
) -> dict[str, str]:
    """stable_id -> the copy holding its vector, given its findings as :func:`findings_for` read them.

    The vector sits beside the provider's ``ok`` finding, so only those holders are looked up.
    """
    holder_of = {
        sid: f.stable_id
        for sid, rows in resolved.items()
        for f in rows
        if f.provider == provider and f.status == findings.STATUS_OK
    }
    holders = sorted(set(holder_of.values()))
    have: set[str] = set()
    for start in range(0, len(holders), _CHUNK):
        chunk = holders[start : start + _CHUNK]
        have.update(
            str(r[0])
            for r in conn.execute(
                "SELECT stable_id FROM genre_vectors WHERE provider = ? AND taxonomy_version = ? "
                f"AND stable_id IN ({','.join('?' * len(chunk))})",
                [provider, taxonomy.TAXONOMY_VERSION, *chunk],
            )
        )
    return {sid: holder for sid, holder in holder_of.items() if holder in have}


def similar_tracks(
    conn: sqlite3.Connection, groups: Groups, stable_id: str, *, limit: int = 20, provider: str = findings.PROVIDER_JEV
) -> list[tuple[str, float]]:
    """The duplicate groups whose genre vector is closest (cosine) to ``stable_id``'s group's.

    Generic find-similar-tracks (issue #11188) and ``GET /api/v1/genres/{sid}/similar``
    both read through here. Each neighbor is one group, named by the copy that
    holds its vector; the track's own duplicates are never neighbors. Raises
    KeyError when the group has no vector, so "no neighbors" is never confused
    with "never measured".
    """
    by_key = vectors(conn, groups, provider)
    target = by_key.get(groups.key(stable_id)) or by_key.get(_own_key(stable_id))
    if target is None:
        raise KeyError(f"track {stable_id} has no {provider} genre vector; run genre inference first")
    # Not a neighbor: the track's group, and any vector one of its copies holds alone.
    mine = {groups.key(stable_id), *(_own_key(member) for member in groups.members_of(stable_id))}
    return findings.nearest({sid: vec for key, (sid, vec) in by_key.items() if key not in mine}, target[1], limit)


# --------------------------------------------------------------- user genres
def _shareable_text(value_json: str | None) -> str | None:
    """A typed genre as non-empty text, only when the stored value is a JSON string.

    A value that cannot be decoded stays on its own row (where the row's own read
    shows it) and is never shown on the track's other copies.
    """
    try:
        value = json.loads(value_json) if value_json else None
    except ValueError:
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _typed_rows(conn: sqlite3.Connection, members: Sequence[str] | None) -> Iterator[tuple[str, str, str]]:
    """(stable_id, typed genre, edited at) for ``members``; None reads every typed genre."""
    typed = (
        "SELECT stable_id, value_json, COALESCE(updated_at, modified_at) FROM track_fields "
        "WHERE field_name = 'genre' AND source = 'webui' AND deleted_at IS NULL"
    )
    chunks = [[]] if members is None else [list(members[i : i + _CHUNK]) for i in range(0, len(members), _CHUNK)]
    for chunk in chunks:
        only = "" if members is None else f" AND stable_id IN ({','.join('?' * len(chunk))})"
        for sid, value_json, edited_at in conn.execute(typed + only, chunk):
            text = _shareable_text(value_json)
            if text is not None:
                yield str(sid), text, str(edited_at or "")


def user_genres(conn: sqlite3.Connection, groups: Groups, stable_ids: Sequence[str]) -> dict[str, str]:
    """stable_id -> the genre a person typed in the Library on any copy of it (the newest edit wins)."""
    wanted = {groups.key(sid) for sid in stable_ids}
    newest: dict[str, tuple[str, str, str]] = {}
    for sid, text, edited_at in _typed_rows(conn, _member_ids(groups, stable_ids)):
        key = groups.key(sid)
        held = newest.get(key)
        # Newest edit first; on a tie the lowest stable_id, so the choice never flips.
        if key in wanted and (held is None or (edited_at, held[1]) > (held[0], sid)):
            newest[key] = (edited_at, sid, text)
    return {sid: newest[groups.key(sid)][2] for sid in stable_ids if groups.key(sid) in newest}


__all__ = [
    "EVIDENCE_IDENTITY",
    "Groups",
    "begin_write",
    "bind_legacy",
    "build",
    "delete_findings",
    "describes_holder",
    "findings_for",
    "load",
    "nameable",
    "resolve",
    "similar_tracks",
    "user_genres",
    "vector_holders",
    "vectors",
]
