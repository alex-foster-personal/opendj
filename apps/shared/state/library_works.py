"""Reads behind version stacks, work suggestions and the classical layout.

Writes live in :mod:`apps.shared.state.library_org`. Everything here only reads,
and nothing here ever links a track: a suggestion is a proposal with a reason
(Library Organization PRD W4, W5).

Composer is deliberately never taken from a track's artist. The artist of a
recording is its performer, and "same artist" is not evidence of the same work
(E38: Schubert's and Gounod's Ave Maria).
"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections.abc import Container
from dataclasses import dataclass
from typing import Any, Literal

from .library_org import list_rows
from .library_org_works import CLASSICAL_FIELD

Confidence = Literal["high", "medium", "low"]

#: A stem shared by more tracks than this is generic ("Intro"), so it proves nothing.
MAX_STEM_BUCKET: int = 25
MIN_STEM_LENGTH: int = 3
#: A work name shorter than this matches too many titles by accident.
MIN_WORK_NAME_LENGTH: int = 4
MAX_SUGGESTIONS: int = 10
MAX_CANDIDATES: int = 30

ROLE_WORDS: dict[str, str] = {
    "original": "original of",
    "recording": "recording of",
    "remix": "remix of",
    "cover": "cover of",
    "edit": "edit of",
    "live": "live",
    "mashup": "mashup of",
}
_CONFIDENCE_RANK: dict[Confidence, int] = {"high": 0, "medium": 1, "low": 2}
_BRACKETS = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_FEATURING = re.compile(r"\b(?:feat|ft|featuring)\b\.?.*$")
_VERSION_TAIL = re.compile(
    r"\s[-–—]\s.*\b(?:remix|mix|edit|version|live|bootleg|rework|flip|vip|dub"
    r"|instrumental|remaster(?:ed)?|acoustic|cover)\b.*$"
)
_NOT_WORD = re.compile(r"[^\w]+")


def role_label(role: str, work_title: str) -> str:
    """The word every member shows (W2): "remix of Lacrimosa", or plain "live"."""
    word = ROLE_WORDS[role]
    return word if role == "live" else f"{word} {work_title}"


def _normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().strip()


def title_stem(title: str | None) -> str:
    """A title without its version wording: what versions of one song share."""
    text = _normalized(title or "")
    text = _BRACKETS.sub(" ", text)
    text = _VERSION_TAIL.sub(" ", text)
    text = _FEATURING.sub(" ", text)
    return " ".join(_NOT_WORD.sub(" ", text).split())


def artist_names(artists_json: str | None) -> str:
    """A track's artists as display text."""
    if not artists_json:
        return ""
    return ", ".join(json.loads(artists_json))


@dataclass(frozen=True)
class _Track:
    stable_id: str
    title: str
    artist: str
    stem: str
    composer: str | None
    catalogue: str | None
    work_id: str | None


@dataclass(frozen=True)
class _Corpus:
    """One snapshot of what suggestions compare."""

    tracks: dict[str, _Track]
    by_stem: dict[str, list[str]]
    by_composition: dict[tuple[str, str], list[str]]
    works: dict[str, dict[str, Any]]
    members: dict[str, set[str]]
    dismissed: set[tuple[str, str]]


def _classical_rows(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT stable_id, value_json FROM track_fields "
        "WHERE field_name = ? AND deleted_at IS NULL",
        (CLASSICAL_FIELD,),
    ).fetchall()
    return {row[0]: json.loads(row[1]) for row in rows}


def _primary_by_track(members: list[dict[str, Any]]) -> dict[str, str]:
    return {
        m["member_id"]: m["work_id"]
        for m in members
        if m["member_kind"] == "track" and m["is_primary"]
    }


_CORPUS_SIGNATURE = (
    "SELECT (SELECT COUNT(*) || ':' || COALESCE(MAX(updated_at), '') FROM tracks "
    "        WHERE deleted_at IS NULL), "
    "(SELECT COUNT(*) || ':' || COALESCE(MAX(updated_at), '') FROM track_fields "
    "        WHERE field_name = ?), "
    "(SELECT COUNT(*) || ':' || COALESCE(MAX(updated_at), '') FROM work), "
    "(SELECT COUNT(*) || ':' || COALESCE(MAX(updated_at), '') FROM work_member), "
    "(SELECT COUNT(*) || ':' || COALESCE(MAX(updated_at), '') FROM work_dismissal)"
)
#: One corpus per database file: every suggestion read would otherwise walk the
#: whole library (E33).
_corpus_cache: dict[str, tuple[tuple[Any, ...], _Corpus]] = {}


def _load_corpus(conn: sqlite3.Connection) -> _Corpus:
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    signature = tuple(conn.execute(_CORPUS_SIGNATURE, (CLASSICAL_FIELD,)).fetchone())
    cached = _corpus_cache.get(path)
    if cached is not None and cached[0] == signature:
        return cached[1]
    corpus = _read_corpus(conn)
    _corpus_cache[path] = (signature, corpus)
    return corpus


def _index_track(
    corpus: _Corpus, stable_id: str, title: str | None, artists_json: str | None,
    values: dict[str, Any], work: dict[str, Any] | None,
) -> None:
    composer = values.get("composer") or (work["composer"] if work else None)
    catalogue = values.get("catalogue") or (work["catalogue"] if work else None)
    track = _Track(
        stable_id, title or "", artist_names(artists_json), title_stem(title),
        composer, catalogue, work["id"] if work else None,
    )
    corpus.tracks[stable_id] = track
    if len(track.stem) >= MIN_STEM_LENGTH:
        corpus.by_stem.setdefault(track.stem, []).append(stable_id)
    if composer and catalogue:
        composition = (_normalized(composer), _normalized(catalogue))
        corpus.by_composition.setdefault(composition, []).append(stable_id)


def _read_corpus(conn: sqlite3.Connection) -> _Corpus:
    classical = _classical_rows(conn)
    works = {w["id"]: w for w in list_rows(conn, "work")}
    members = list_rows(conn, "work_member")
    primary = _primary_by_track(members)
    grouped: dict[str, set[str]] = {}
    for member in members:
        grouped.setdefault(member["work_id"], set()).add(member["member_id"])
    dismissed = {
        (row["a_member"], row["b_member"]) for row in list_rows(conn, "work_dismissal")
    }
    corpus = _Corpus({}, {}, {}, works, grouped, dismissed)
    for stable_id, title, artists_json in conn.execute(
        "SELECT stable_id, title, artists_json FROM tracks WHERE deleted_at IS NULL"
    ).fetchall():
        work = works.get(primary.get(stable_id, ""))
        _index_track(corpus, stable_id, title, artists_json, classical.get(stable_id, {}), work)
    return corpus


def _composers_differ(a: str | None, b: str | None) -> bool:
    return bool(a and b) and _normalized(a or "") != _normalized(b or "")


def _is_dismissed(corpus: _Corpus, a: str, b: str) -> bool:
    return tuple(sorted((a, b))) in corpus.dismissed


def _stem_confidence(track: _Track, other: _Track) -> tuple[Confidence, str]:
    if track.composer and other.composer:
        return "high", f"same title stem and composer ({track.composer})"
    if track.composer or other.composer:
        return "medium", "same title stem; composer known on one side only"
    if track.artist and track.artist == other.artist:
        return "medium", "same title stem and artist"
    return "low", "same title stem"


def _suggestion(
    other: _Track | None,
    work: dict[str, Any] | None,
    code: str,
    confidence: Confidence,
    reason: str,
) -> dict[str, Any]:
    return {
        "member_id": other.stable_id if other else None,
        "title": other.title if other else None,
        "artist": other.artist if other else None,
        "work_id": work["id"] if work else None,
        "work_title": work["title"] if work else None,
        "reason_code": code,
        "reason": reason,
        "confidence": confidence,
    }


def _composition_matches(corpus: _Corpus, track: _Track) -> dict[str, dict[str, Any]]:
    if not (track.composer and track.catalogue):
        return {}
    composition = (_normalized(track.composer), _normalized(track.catalogue))
    found: dict[str, dict[str, Any]] = {}
    for other_id in corpus.by_composition.get(composition, []):
        other = corpus.tracks[other_id]
        if other_id != track.stable_id and not _is_dismissed(corpus, track.stable_id, other_id):
            work = corpus.works.get(other.work_id or "")
            found[other_id] = _suggestion(
                other, work, "composer_catalogue", "high",
                f"same composer and catalogue ({track.composer}, {track.catalogue})",
            )
    return found


def _stem_matches(
    corpus: _Corpus, track: _Track, skip: Container[str]
) -> dict[str, dict[str, Any]]:
    bucket = corpus.by_stem.get(track.stem, [])
    found: dict[str, dict[str, Any]] = {}
    if len(bucket) > MAX_STEM_BUCKET:
        return found
    for other_id in bucket:
        other = corpus.tracks[other_id]
        if (
            other_id == track.stable_id
            or other_id in skip
            or _composers_differ(track.composer, other.composer)
            or _is_dismissed(corpus, track.stable_id, other_id)
        ):
            continue
        confidence, reason = _stem_confidence(track, other)
        work = corpus.works.get(other.work_id or "")
        found[other_id] = _suggestion(other, work, "title_stem", confidence, reason)
    return found


def _track_suggestions(corpus: _Corpus, track: _Track) -> dict[str, dict[str, Any]]:
    found = _composition_matches(corpus, track)
    return {**found, **_stem_matches(corpus, track, found)}


def _work_suggestions(corpus: _Corpus, track: _Track) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    padded = f" {track.stem} "
    for work_id, work in corpus.works.items():
        name = title_stem(work["title"])
        if (
            len(name) < MIN_WORK_NAME_LENGTH
            or f" {name} " not in padded
            or track.work_id == work_id
            or _composers_differ(track.composer, work["composer"])
            or _is_dismissed(corpus, track.stable_id, f"work:{work_id}")
        ):
            continue
        confidence: Confidence = "high" if track.composer and work["composer"] else "medium"
        found.append(_suggestion(
            None, work, "title_contains_work", confidence,
            f"title contains the work name {work['title']!r}",
        ))
    return found


def _suggestions_for(corpus: _Corpus, stable_id: str) -> list[dict[str, Any]]:
    track = corpus.tracks.get(stable_id)
    if track is None:
        return []
    candidates = [
        s for s in _track_suggestions(corpus, track).values()
        if track.work_id is None or s["work_id"] != track.work_id
    ]
    candidates.extend(_work_suggestions(corpus, track))
    candidates.sort(key=lambda s: (_CONFIDENCE_RANK[s["confidence"]], s["title"] or ""))
    return candidates[:MAX_SUGGESTIONS]


def suggest(conn: sqlite3.Connection, stable_id: str) -> list[dict[str, Any]]:
    """Proposed versions of a track, each with a confidence and a reason. Links nothing."""
    return _suggestions_for(_load_corpus(conn), stable_id)


def suggestion_flags(conn: sqlite3.Connection, stable_ids: list[str]) -> list[str]:
    """Which of these tracks have at least one suggestion: the ``▸ ?`` indicator."""
    corpus = _load_corpus(conn)
    return [sid for sid in stable_ids if _suggestions_for(corpus, sid)]


def _candidate_tracks(corpus: _Corpus, track: _Track, needle: str) -> list[_Track]:
    def matches(item: _Track) -> bool:
        return not needle or needle in _normalized(f"{item.title} {item.artist}")

    same_stem = [
        corpus.tracks[sid] for sid in corpus.by_stem.get(track.stem, [])
        if sid != track.stable_id and matches(corpus.tracks[sid])
    ]
    if not needle:
        return same_stem
    seen = {item.stable_id for item in same_stem} | {track.stable_id}
    others = [item for item in corpus.tracks.values() if item.stable_id not in seen and matches(item)]
    return [*same_stem, *sorted(others, key=lambda item: item.title)]


def link_candidates(conn: sqlite3.Connection, stable_id: str, query: str) -> list[dict[str, Any]]:
    """The "Link a version..." list: same-stem titles first, then library matches."""
    corpus = _load_corpus(conn)
    track = corpus.tracks.get(stable_id)
    if track is None:
        return []
    return [
        {"member_id": item.stable_id, "title": item.title, "artist": item.artist,
         "same_stem": item.stem == track.stem}
        for item in _candidate_tracks(corpus, track, _normalized(query))[:MAX_CANDIDATES]
    ]


def overlay(conn: sqlite3.Connection) -> dict[str, Any]:
    """What the table draws: every work, each track's links, and classical values.

    A track in no work and with no classical metadata is simply absent, so an
    unorganized library costs nothing and shows nothing (W7).
    """
    members = list_rows(conn, "work_member")
    counts: dict[str, int] = {}
    links: dict[str, list[dict[str, Any]]] = {}
    for member in members:
        counts[member["work_id"]] = counts.get(member["work_id"], 0) + 1
        links.setdefault(member["member_id"], []).append({
            "work_id": member["work_id"],
            "role": member["role"],
            "is_primary": bool(member["is_primary"]),
        })
    works = {
        w["id"]: {
            "kind": w["kind"], "title": w["title"], "composer": w["composer"],
            "catalogue": w["catalogue"], "movement": w["movement"],
            "aliases": json.loads(w["aliases"]), "member_count": counts.get(w["id"], 0),
        }
        for w in list_rows(conn, "work")
    }
    return {"works": works, "members": links, "classical": _classical_rows(conn)}


def _stack_order(work_id: str, conn: sqlite3.Connection) -> list[dict[str, Any]]:
    members = sorted(
        list_rows(conn, "work_member", {"work_id": work_id}),
        key=lambda m: (m["sort_index"], m["member_id"]),
    )
    prefs = list_rows(conn, "work_pref", {"work_id": work_id})
    top = prefs[0]["top_member_id"] if prefs else None
    return sorted(members, key=lambda m: m["member_id"] != top)


def _places(conn: sqlite3.Connection, playlist_id: str | None) -> dict[str, int]:
    """Each track's 1-based row in a list. Positions may have gaps, so rank them."""
    if playlist_id is None:
        return {}
    rows = conn.execute(
        "SELECT stable_id FROM playlist_memberships "
        "WHERE playlist_id = ? AND deleted_at IS NULL ORDER BY position",
        (playlist_id,),
    ).fetchall()
    places: dict[str, int] = {}
    for index, row in enumerate(rows, start=1):
        places.setdefault(row[0], index)
    return places


def stack(
    conn: sqlite3.Connection, stable_id: str, playlist_id: str | None = None
) -> dict[str, Any]:
    """Everything an expanded stack shows, opened from one playlist row."""
    links = list_rows(conn, "work_member", {"member_kind": "track", "member_id": stable_id})
    primary = next((m for m in links if m["is_primary"]), links[0] if links else None)
    work = None
    rows: list[dict[str, Any]] = []
    if primary is not None:
        found = list_rows(conn, "work", {"id": primary["work_id"]})
        work = found[0] if found else None
    if work is not None:
        places = _places(conn, playlist_id)
        for member in _stack_order(work["id"], conn):
            track = conn.execute(
                "SELECT title, artists_json FROM tracks WHERE stable_id = ?",
                (member["member_id"],),
            ).fetchone()
            rows.append({
                "member_kind": member["member_kind"],
                "member_id": member["member_id"],
                "title": track[0] if track else None,
                "artist": artist_names(track[1]) if track else "",
                "role": member["role"],
                "role_label": role_label(member["role"], work["title"]),
                "is_primary": bool(member["is_primary"]),
                "this_row": member["member_id"] == stable_id,
                "place": places.get(member["member_id"]),
            })
    return {"work": work, "members": rows, "suggestions": suggest(conn, stable_id)}
