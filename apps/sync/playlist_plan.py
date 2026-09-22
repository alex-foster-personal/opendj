"""Pure plan builder for Phase 3 playlist sync (SYNC-03).

This module contains no I/O side effects (aside from reading a CSV file
inside :func:`load_match_set`). It takes flattened RB playlists + djay
playlists + a match-set, and produces a :class:`PlaylistPlan` tree that the
CLI/artefact writers (``apps.sync.playlist_diff``) serialise.

Design decisions inherited from ``.planning/phases/03-playlist-sync/03-CONTEXT.md``:

* D1: Rekordbox is canonical; djay is reconciled to RB.
* D2: only tracks with a confirmed Phase 2 match are written; unmatched
  tracks are surfaced per playlist but excluded from writes.
* D3: RB-canonical; djay-only playlists are reported as ``noop`` (left alone).
* D6: flatten the RB folder hierarchy into dotted names with " / " separator.

The match-set CSV schema is the Phase 2 contract: each row carries
``rb_id,djay_uuid,confidence,signals_fired`` (plus an ignored tail). The
confirmed threshold is ``confidence >= 0.70 AND signals_fired >= 3``. If
Phase 2 has not shipped yet, tests exercise the loader against a hand-rolled
CSV with the same schema; parent orchestrator wires the real ``matches.csv``
in the central sweep.
"""
from __future__ import annotations

import csv
import hashlib
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from apps.shared.djay_db import DjayPlaylist
from apps.shared.rekordbox_db import RBPlaylist


class PlaylistPlanError(ValueError):
    """Raised when plan inputs violate invariants (e.g. collisions)."""


# ----- Thresholds / constants -------------------------------------------

#: Confidence floor for a confirmed Phase 2 match.
CONFIRMED_CONFIDENCE = 0.70
#: Minimum number of matcher signals required for a confirmed match.
CONFIRMED_SIGNALS = 3
#: Separator used when flattening RB folder paths to djay names (D6).
FOLDER_SEP = " / "


# ----- Dataclasses -------------------------------------------------------


@dataclass(slots=True, frozen=True)
class FlatPlaylist:
    """A flattened RB playlist ready for the diff step."""

    rb_id: str
    flat_name: str
    parent_path: tuple[str, ...]
    track_ids_ordered: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class DjayPlaylistRead:
    """A djay playlist as read by the diff step (subset of DjayPlaylist)."""

    uuid: str
    name: str
    member_uuids_ordered: tuple[str, ...]


@dataclass(slots=True)
class MatchSet:
    """Confirmed Phase 2 matches, with bidirectional maps and provenance."""

    rb_to_djay: dict[str, str] = field(default_factory=dict)
    djay_to_rb: dict[str, str] = field(default_factory=dict)
    source_sha256: str = ""
    source_path: str = ""

    @property
    def confirmed_rb_ids(self) -> set[str]:
        return set(self.rb_to_djay.keys())

    @property
    def confirmed_djay_uuids(self) -> set[str]:
        return set(self.djay_to_rb.keys())


@dataclass(slots=True, frozen=True)
class PlannedMember:
    """One entry in a planned playlist membership (matched-only)."""

    track_no: int
    rb_id: str
    djay_uuid: str


@dataclass(slots=True, frozen=True)
class UnmatchedTrack:
    """An RB track that has no confirmed djay counterpart."""

    rb_id: str
    title: str
    artist: str


@dataclass(slots=True)
class PlaylistOp:
    """A per-playlist planned operation.

    ``op`` is one of ``create``, ``update``, ``noop``. Adds/removes/reorders
    are computed against the djay-current membership for ``update`` ops;
    they are empty for ``create`` ops (target is the full set).
    """

    rb_id: str
    rb_name: str
    op: str  # "create" | "update" | "noop"
    djay_uuid: str | None
    djay_name_current: str | None
    target_members: list[PlannedMember] = field(default_factory=list)
    djay_current_members: list[str] = field(default_factory=list)
    adds: list[PlannedMember] = field(default_factory=list)
    removes: list[str] = field(default_factory=list)  # djay_uuids to remove
    reordered: bool = False
    unmatched_rb: list[UnmatchedTrack] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class DjayOnlyPlaylist:
    """A djay playlist with no RB counterpart after canonical-name collation."""

    djay_uuid: str
    name: str
    member_count: int


@dataclass(slots=True)
class PlaylistPlan:
    """Full Phase 3 plan tree."""

    generated_at: str
    match_set_sha256: str
    playlists: list[PlaylistOp] = field(default_factory=list)
    djay_only: list[DjayOnlyPlaylist] = field(default_factory=list)


# ----- RB hierarchy flattener -------------------------------------------


def _canonical_name(name: str) -> str:
    """Collation key used for RB<->djay playlist-name matching (D6)."""
    return unicodedata.normalize("NFC", name).casefold().strip()


def flatten_rb_playlists(
    playlists: Iterable[RBPlaylist],
    *,
    streaming_filter: dict[str, bool] | None = None,
) -> list[FlatPlaylist]:
    """Flatten nested RB playlists to dotted names, filter streaming-only.

    Parameters
    ----------
    playlists
        Full RB playlist iterable (``iter_playlists(db)``). Folder nodes
        with :attr:`track_ids` empty are treated as folders; leaves keep
        their membership.
    streaming_filter
        Optional ``{rb_track_id: is_streaming}`` map. When supplied, any
        track flagged as streaming is dropped from ``track_ids_ordered``.
        Streaming-only playlists (all members streaming) end up with an
        empty ``track_ids_ordered`` tuple.

    Notes
    -----
    * Folder-only nodes with no tracks and no descendants are filtered out.
    * Track order is preserved from RB (already sorted by ``TrackNo`` by
      :func:`apps.shared.rekordbox_db.iter_playlists`); ties are stable
      because pyrekordbox preserves insertion order within equal ``TrackNo``.
    * Names are NFC-normalised for internal comparison but the RB casing is
      preserved on output (CONTEXT D6).
    """
    by_id: dict[str, RBPlaylist] = {p.id: p for p in playlists}
    if not by_id:
        return []

    # Build the dotted path for each node; memoise.
    path_cache: dict[str, tuple[str, ...]] = {}

    def _path_for(pid: str) -> tuple[str, ...]:
        if pid in path_cache:
            return path_cache[pid]
        node = by_id.get(pid)
        if node is None:
            path_cache[pid] = ()
            return ()
        if node.parent_id is None or node.parent_id == pid:
            result: tuple[str, ...] = (node.name,)
        else:
            parent_path = _path_for(node.parent_id)
            result = (*parent_path, node.name)
        path_cache[pid] = result
        return result

    out: list[FlatPlaylist] = []
    for node in by_id.values():
        path = _path_for(node.id)
        if not path:
            continue
        ids = tuple(
            tid
            for tid in node.track_ids
            if not (streaming_filter or {}).get(tid, False)
        )
        # Folder nodes have no direct track members in pyrekordbox; they are
        # skipped here by the "empty ids + name already a prefix of a child"
        # test. We use a simple heuristic: if this node has no tracks AND at
        # least one other node lists it as parent, treat it as a folder and
        # skip. Otherwise keep it (could be a legitimately empty playlist).
        if not ids:
            has_children = any(
                other.parent_id == node.id for other in by_id.values()
            )
            if has_children:
                continue
        out.append(
            FlatPlaylist(
                rb_id=node.id,
                flat_name=FOLDER_SEP.join(path),
                parent_path=path[:-1],
                track_ids_ordered=ids,
            )
        )
    # Deterministic output order: by canonical name then rb_id.
    out.sort(key=lambda fp: (_canonical_name(fp.flat_name), fp.rb_id))
    return out


def read_djay_playlists(djay_playlists: Iterable[DjayPlaylist]) -> list[DjayPlaylistRead]:
    """Project djay playlists to the minimal shape the diff step needs.

    Wraps :func:`apps.shared.djay_db.iter_playlists` output; also drops the
    ``mediaItemPlaylist-root`` sentinel from the comparison set (it is
    always present and carries the full library, not a user playlist).
    """
    out: list[DjayPlaylistRead] = []
    for p in djay_playlists:
        if p.uuid == "mediaItemPlaylist-root":
            # Root is a system container; do not match it against RB.
            continue
        out.append(
            DjayPlaylistRead(
                uuid=p.uuid,
                name=p.name or "",
                member_uuids_ordered=tuple(p.track_uuids),
            )
        )
    return out


# ----- Match-set loader --------------------------------------------------


def load_match_set(path: Path) -> MatchSet:
    """Load ``matches.csv`` and retain only rows that meet the Phase 2 bar.

    Raises
    ------
    FileNotFoundError
        If ``path`` does not exist. Message includes a hint to re-run the
        Phase 2 matcher (``python -m apps.sync.matcher ...``).
    """
    if not path.exists():
        raise FileNotFoundError(
            f"matches.csv not found at {path}. Run the Phase 2 matcher first: "
            "`python -m apps.sync.matcher --out data/sync/matches.csv`."
        )

    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()

    rb_to_djay: dict[str, str] = {}
    djay_to_rb: dict[str, str] = {}

    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rb_id = (row.get("rb_id") or "").strip()
            djay_uuid = (row.get("djay_uuid") or "").strip()
            if not rb_id or not djay_uuid:
                continue
            try:
                confidence = float(row.get("confidence") or 0.0)
            except (TypeError, ValueError):
                continue
            try:
                signals = int(row.get("signals_fired") or 0)
            except (TypeError, ValueError):
                continue
            if confidence < CONFIRMED_CONFIDENCE:
                continue
            if signals < CONFIRMED_SIGNALS:
                continue
            # First occurrence wins; duplicates are a matcher bug but we do
            # not crash Phase 3 over them.
            rb_to_djay.setdefault(rb_id, djay_uuid)
            djay_to_rb.setdefault(djay_uuid, rb_id)

    return MatchSet(
        rb_to_djay=rb_to_djay,
        djay_to_rb=djay_to_rb,
        source_sha256=sha,
        source_path=str(path),
    )


# ----- Plan builder ------------------------------------------------------


def build_plan(
    rb_playlists: Iterable[FlatPlaylist],
    djay_playlists: Iterable[DjayPlaylistRead],
    matches: MatchSet,
    *,
    rb_track_titles: dict[str, tuple[str, str]] | None = None,
    generated_at: str | None = None,
) -> PlaylistPlan:
    """Build a :class:`PlaylistPlan` (RB-canonical, CONTEXT D1..D3, D6).

    ``rb_track_titles`` is an optional ``{rb_id: (title, artist)}`` map used
    to populate the ``unmatched_rb`` section of each op; if omitted, title
    and artist are blank strings.

    ``generated_at`` lets callers inject a stable timestamp for golden-file
    tests.
    """
    titles = rb_track_titles or {}

    # Index djay playlists by canonical name.
    # P03-04: canonical-name collisions (e.g. NFC vs NFD duplicates, or two
    # djay playlists that differ only in case) used to silently overwrite one
    # another in this dict, collapsing two distinct djay playlists into one
    # plan op. Detect collisions and surface them as a PlaylistPlanError so
    # the operator can resolve the ambiguity in djay before planning.
    djay_by_canon: dict[str, DjayPlaylistRead] = {}
    collisions: dict[str, list[str]] = {}
    for dp in djay_playlists:
        canon = _canonical_name(dp.name)
        existing = djay_by_canon.get(canon)
        if existing is not None and existing.uuid != dp.uuid:
            collisions.setdefault(canon, [existing.name]).append(dp.name)
            continue
        djay_by_canon[canon] = dp
    if collisions:
        details = "; ".join(
            f"{canon!r} <- {sorted(set(names))}"
            for canon, names in sorted(collisions.items())
        )
        raise PlaylistPlanError(
            "djay playlists collide on canonical name (P03-04): " + details
        )

    seen_djay_uuids: set[str] = set()
    ops: list[PlaylistOp] = []

    for fp in rb_playlists:
        canon = _canonical_name(fp.flat_name)
        target_members: list[PlannedMember] = []
        unmatched: list[UnmatchedTrack] = []
        for idx, rb_id in enumerate(fp.track_ids_ordered, start=1):
            djay_uuid = matches.rb_to_djay.get(rb_id)
            if djay_uuid:
                target_members.append(
                    PlannedMember(track_no=idx, rb_id=rb_id, djay_uuid=djay_uuid)
                )
            else:
                title, artist = titles.get(rb_id, ("", ""))
                unmatched.append(
                    UnmatchedTrack(rb_id=rb_id, title=title, artist=artist)
                )

        dp = djay_by_canon.get(canon)
        if dp is None:
            op = PlaylistOp(
                rb_id=fp.rb_id,
                rb_name=fp.flat_name,
                op="create",
                djay_uuid=None,
                djay_name_current=None,
                target_members=target_members,
                djay_current_members=[],
                adds=list(target_members),
                removes=[],
                reordered=False,
                unmatched_rb=unmatched,
            )
        else:
            seen_djay_uuids.add(dp.uuid)
            current = list(dp.member_uuids_ordered)
            current_set = set(current)
            target_uuids = [m.djay_uuid for m in target_members]
            target_set = set(target_uuids)

            adds = [m for m in target_members if m.djay_uuid not in current_set]
            removes = [u for u in current if u not in target_set]
            # Reordered iff the intersection order differs.
            current_intersection = [u for u in current if u in target_set]
            target_intersection = [u for u in target_uuids if u in current_set]
            reordered = (
                not adds
                and not removes
                and current_intersection != target_intersection
                and bool(current_intersection)
            )
            no_change = not adds and not removes and not reordered
            op = PlaylistOp(
                rb_id=fp.rb_id,
                rb_name=fp.flat_name,
                op="noop" if no_change else "update",
                djay_uuid=dp.uuid,
                djay_name_current=dp.name,
                target_members=target_members,
                djay_current_members=current,
                adds=adds,
                removes=removes,
                reordered=reordered,
                unmatched_rb=unmatched,
            )
        ops.append(op)

    # djay-only playlists (D3: noop, reported).
    djay_only: list[DjayOnlyPlaylist] = []
    for dp in djay_playlists:
        if dp.uuid in seen_djay_uuids:
            continue
        djay_only.append(
            DjayOnlyPlaylist(
                djay_uuid=dp.uuid,
                name=dp.name,
                member_count=len(dp.member_uuids_ordered),
            )
        )
    djay_only.sort(key=lambda d: (_canonical_name(d.name), d.djay_uuid))

    return PlaylistPlan(
        generated_at=generated_at or "",
        match_set_sha256=matches.source_sha256,
        playlists=ops,
        djay_only=djay_only,
    )


__all__ = [
    "CONFIRMED_CONFIDENCE",
    "CONFIRMED_SIGNALS",
    "FOLDER_SEP",
    "FlatPlaylist",
    "DjayPlaylistRead",
    "MatchSet",
    "PlannedMember",
    "UnmatchedTrack",
    "PlaylistOp",
    "DjayOnlyPlaylist",
    "PlaylistPlan",
    "PlaylistPlanError",
    "flatten_rb_playlists",
    "read_djay_playlists",
    "load_match_set",
    "build_plan",
]
