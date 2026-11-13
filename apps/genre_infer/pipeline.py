"""Infer each track's genre: cached web search -> JEV + DeepSeek -> fused finding in state.db.

One run (:func:`infer_tracks`) for a list of stable_ids:

0. Duplicates are asked ONCE (GENRE-08): the requested tracks are folded into
   duplicate groups (:mod:`apps.genre_infer.groups`), one member is asked, and
   every other requested member shares its result at no cost.
1. Build each track's text: what the library knows (``jev.build_state``), up to
   10 of the artist's other library titles (never their genres), then the web
   search snippets for the frozen query (from ``genre_search_cache`` when the
   query was searched before, else one paid search).
2. If the stored ``odj-fused`` finding was made from the same request digest
   (text + taxonomy + models + prompt version), the track is ``up_to_date`` and
   costs nothing. ``force`` re-asks anyway.
3. Ask JEV and DeepSeek concurrently, fuse (:func:`providers.fuse`) and store
   three findings (``jev``, ``deepseek``, ``odj-fused``) plus JEV's full vector.

Network work runs on a thread pool; every database write happens on the caller's
connection, on the caller's thread, after the calls return.

-Claude
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from apps.adapters.rekordbox import config as rb_config
from apps.shared.non_music import is_non_music
from apps.shared.state.analysis_queue_ids import ITEM_PENDING, ITEM_RUNNING, USER_BATCH_IDS
from apps.shared.state.locations import bulk_local_audio_paths

from . import display, findings, groups, jev, jev_store, library_tags, providers, taxonomy

log = logging.getLogger("apps.genre_infer.pipeline")

OTHER_TITLES_MAX = 10
DEFAULT_WORKERS = 8
# Owner constraint (Mon 12 Oct 2026): under $1 per 1,000 tracks.
COST_CEILING_USD_PER_1K = 1.0
OUTCOME_DONE = "done"
OUTCOME_UP_TO_DATE = "up_to_date"
OUTCOME_SKIPPED = "skipped"
OUTCOME_FAILED = "failed"
OUTCOME_CANCELLED = "cancelled"
SKIP_NO_ARTIST_TITLE = "no_artist_title"
SKIP_NOT_A_TRACK = "not_in_library"
SKIP_HAS_CHOSEN_GENRE = "has_chosen_genre"
SKIP_GENRE_JOB_ACTIVE = "genre_job_active"
# The track's title or artists changed while the models were being asked about its old ones.
FAIL_RETAGGED = "retagged while it was being asked; ask again"


@dataclass(frozen=True)
class TrackResult:
    stable_id: str
    outcome: str
    reason: str | None = None
    genre: str | None = None
    confidence: float | None = None
    rule: str | None = None
    cost_usd: float = 0.0
    paid_search: bool = False
    search_cost_usd: float = 0.0
    #: GENRE-08: the requested duplicate whose ask this track shares; nothing was paid for this one.
    shared_from: str | None = None


@dataclass
class RunSummary:
    results: list[TrackResult] = field(default_factory=list)

    def count(self, outcome: str) -> int:
        return sum(1 for r in self.results if r.outcome == outcome)

    @property
    def cost_usd(self) -> float:
        return round(sum(r.cost_usd for r in self.results), 6)

    @property
    def paid_searches(self) -> int:
        return sum(1 for r in self.results if r.paid_search)

    @property
    def search_cost_usd(self) -> float:
        return round(sum(r.search_cost_usd for r in self.results), 6)

    @property
    def asked(self) -> int:
        """Tracks a call was made for: a duplicate sharing another track's ask is not one."""
        return sum(
            1
            for r in self.results
            if r.shared_from is None and r.outcome in (OUTCOME_DONE, OUTCOME_FAILED, OUTCOME_CANCELLED)
        )

    @property
    def shared(self) -> int:
        """Requested tracks that shared a duplicate's ask instead of paying for their own."""
        return sum(1 for r in self.results if r.shared_from is not None)

    def _per_1k(self, usd: float) -> float | None:
        return None if self.asked == 0 else round(1000 * usd / self.asked, 4)

    @property
    def cost_per_1k_usd(self) -> float | None:
        """Measured model cost plus searches at the configured price, per 1,000 tracks asked."""
        return self._per_1k(self.cost_usd)

    @property
    def model_cost_per_1k_usd(self) -> float | None:
        """The models alone (as billed by OpenRouter), per 1,000 tracks asked."""
        return self._per_1k(self.cost_usd - self.search_cost_usd)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tracks": len(self.results),
            "done": self.count(OUTCOME_DONE),
            "up_to_date": self.count(OUTCOME_UP_TO_DATE),
            "skipped": self.count(OUTCOME_SKIPPED),
            "failed": self.count(OUTCOME_FAILED),
            "cancelled": self.count(OUTCOME_CANCELLED),
            "asked": self.asked,
            "shared_with_duplicates": self.shared,
            "paid_searches": self.paid_searches,
            "cost_usd": self.cost_usd,
            "search_cost_usd": self.search_cost_usd,
            "search_usd_per_search": providers.serper_usd_per_search(),
            "cost_per_1k_usd": self.cost_per_1k_usd,
            "model_cost_per_1k_usd": self.model_cost_per_1k_usd,
            "cost_ceiling_usd_per_1k": COST_CEILING_USD_PER_1K,
            "results": [r.__dict__ for r in self.results],
        }


@dataclass(frozen=True)
class _Context:
    stable_id: str
    artist: str
    title: str
    base_state: str
    #: GENRE-08: the requested tracks this ask answers, the payer first (``stable_id`` may not be one of them).
    requested: tuple[str, ...] = ()
    #: The duplicate group the ask was planned for. Membership is read again when the
    #: result is stored: a retag can move a track while the calls are in flight.
    group_key: str = ""


# ------------------------------------------------------------------ context
def _other_titles(conn: sqlite3.Connection, facts: Mapping[str, Mapping[str, Any]]) -> dict[str, list[str]]:
    """stable_id -> up to 10 'artist - title' lines of the same artist's other live tracks."""
    artists = {str(f["artist"]) for f in facts.values() if f.get("artist")}
    by_artist: dict[str, list[tuple[str, str]]] = {}
    if artists:
        for sid, title, artists_json in conn.execute(
            "SELECT stable_id, title, artists_json FROM tracks WHERE deleted_at IS NULL ORDER BY stable_id"
        ):
            name = jev_store.artist_name(artists_json)
            if name in artists and title:
                by_artist.setdefault(name, []).append((sid, f"{name} - {title}"))
    return {
        sid: [line for other, line in by_artist.get(str(f.get("artist")), []) if other != sid][:OTHER_TITLES_MAX]
        for sid, f in facts.items()
    }


def _artist_title(facts: Mapping[str, Any]) -> tuple[str, str]:
    return str(facts.get("artist") or "").strip(), str(facts.get("title") or "").strip()


def _searchable(artist: str, title: str) -> bool:
    return groups.nameable(artist, title)


def searchable_ids(conn: sqlite3.Connection, stable_ids: Sequence[str]) -> list[str]:
    """The ids whose CURRENT artist and title are real (not empty, ``ID`` or ``Unknown``).

    Read at each call, so a track whose metadata is fixed later becomes searchable.
    """
    facts = jev_store.track_facts(conn, stable_ids)
    return [sid for sid in stable_ids if sid in facts and _searchable(*_artist_title(facts[sid]))]


def _contexts(conn: sqlite3.Connection, stable_ids: Sequence[str]) -> tuple[dict[str, _Context], dict[str, str]]:
    """(contexts, skip reasons) for ``stable_ids``."""
    facts = jev_store.track_facts(conn, stable_ids)
    others = _other_titles(conn, facts)
    contexts: dict[str, _Context] = {}
    skipped: dict[str, str] = {}
    for sid in stable_ids:
        f = facts.get(sid)
        if f is None:
            skipped[sid] = SKIP_NOT_A_TRACK
            continue
        artist, title = _artist_title(f)
        if not _searchable(artist, title):
            skipped[sid] = SKIP_NO_ARTIST_TITLE
            continue
        state = jev.build_state(f)
        if others[sid]:
            state += "\nOther tracks by this artist in the library:\n" + "\n".join(others[sid])
        contexts[sid] = _Context(sid, artist, title, state)
    return contexts, skipped


def _bind_legacy(conn: sqlite3.Connection) -> None:
    """Give findings from before identities were recorded their identity, in one committed write."""
    began = groups.begin_write(conn)
    groups.bind_legacy(conn, groups.load(conn))
    if began:
        conn.commit()


def _plan(conn: sqlite3.Connection, stable_ids: Sequence[str]) -> tuple[dict[str, _Context], dict[str, str]]:
    """(one context per duplicate group, skip reasons) for ``stable_ids`` (GENRE-08).

    The member asked is the copy that already holds the group's fused finding,
    so its stored request digest is compared with its own text; with no finding
    yet it is the first requested member.
    """
    contexts, skipped = _contexts(conn, stable_ids)
    grp = groups.load(conn)
    requested: dict[str, list[str]] = {}
    for sid in contexts:
        requested.setdefault(grp.key(sid), []).append(sid)
    firsts = [members[0] for members in requested.values()]
    stored = groups.findings_for(conn, grp, firsts, providers=[findings.PROVIDER_FUSED]) if firsts else {}
    holder_of = {sid: rows[0].stable_id for sid, rows in stored.items()}
    outside = [holder for holder in holder_of.values() if holder not in contexts]
    held_contexts = _contexts(conn, outside)[0] if outside else {}
    plan: dict[str, _Context] = {}
    for members in requested.values():
        holder = holder_of.get(members[0], members[0])
        ctx = contexts.get(holder) or held_contexts.get(holder) or contexts[members[0]]
        plan[ctx.stable_id] = replace(ctx, requested=tuple(members), group_key=grp.key(ctx.stable_id))
    return plan, skipped


def full_state(ctx: _Context, snippets: str) -> str:
    return ctx.base_state + ("\nWeb search results for this track:\n" + snippets if snippets.strip() else "")


def request_digest(state: str) -> str:
    doc = {
        "state": state,
        "taxonomy": taxonomy.TAXONOMY_VERSION,
        "prompt": providers.PROMPT_VERSION,
        "jev": providers.JEV_MODEL,
        "deepseek": providers.DEEPSEEK_MODEL,
        "query": providers.QUERY_VERSION,
    }
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


# --------------------------------------------------------------------- run
@dataclass(frozen=True)
class _Asked:
    ctx: _Context
    snippets: str | None
    paid_search: bool
    jev: providers.Answer | None = None
    deepseek: providers.Answer | None = None
    error: str | None = None


@dataclass(frozen=True)
class _Searched:
    """One query's search outcome: text, or an error; ``paid`` when the call was billed."""

    text: str | None
    paid: bool
    error: str | None = None


def _search(query: str, key: str) -> _Searched:
    """One billable search. Runs on a worker thread: no database."""
    try:
        return _Searched(providers.web_search(query, key), True)
    except providers.ResponseInvalid as exc:
        # The search was billed; only its body was unusable.
        return _Searched(None, True, f"search failed: {exc}")
    except providers.ProviderCallError as exc:
        return _Searched(None, False, f"search failed: {exc}")


def _ask(
    ctx: _Context,
    searched: _Searched,
    paid_here: bool,
    keys: Mapping[str, str],
    force_ask: bool,
    fused_digest: str | None,
) -> _Asked:
    """JEV and DeepSeek over the track's text. Runs on a worker thread: no database.

    ``paid_here`` is True for exactly one track per freshly searched query, so a
    search shared by duplicates is costed once.
    """
    if searched.text is None:
        return _Asked(ctx, None, paid_here, error=searched.error)
    state = full_state(ctx, searched.text)
    if not force_ask and fused_digest == request_digest(state):
        return _Asked(ctx, searched.text, paid_here)
    jev_answer = providers.JevProvider(keys["openrouter"]).answer(state)
    ds_answer = providers.DeepSeekProvider(keys["openrouter"]).answer(state)
    return _Asked(ctx, searched.text, paid_here, jev_answer, ds_answer)


def _store(conn: sqlite3.Connection, asked: _Asked) -> TrackResult:
    sid = asked.ctx.stable_id
    search_cost = providers.serper_usd_per_search() if asked.paid_search else 0.0
    if asked.paid_search and asked.snippets is not None:
        findings.put_search(
            conn, providers.search_query(asked.ctx.artist, asked.ctx.title), providers.QUERY_VERSION, asked.snippets
        )
    if asked.error is not None:
        return TrackResult(
            sid,
            OUTCOME_FAILED,
            asked.error,
            cost_usd=search_cost,
            paid_search=asked.paid_search,
            search_cost_usd=search_cost,
        )
    if asked.jev is None or asked.deepseek is None:
        return TrackResult(
            sid, OUTCOME_UP_TO_DATE, cost_usd=search_cost, paid_search=asked.paid_search, search_cost_usd=search_cost
        )
    return _store_answers(conn, asked, asked.jev, asked.deepseek, search_cost)


def _finding(sid: str, answer: providers.Answer | providers.Fused, **fields: Any) -> findings.Finding:
    return findings.Finding(
        stable_id=sid,
        status=findings.STATUS_OK if answer.genre else findings.STATUS_UNKNOWN,
        genre=answer.genre,
        confidence=answer.confidence,
        version=f"{taxonomy.TAXONOMY_VERSION} {providers.PROMPT_VERSION}",
        **fields,
    )


def _store_answers(
    conn: sqlite3.Connection, asked: _Asked, jev: providers.Answer, deepseek: providers.Answer, search_cost: float
) -> TrackResult:
    sid = asked.ctx.stable_id
    llm_cost = sum(a.cost_usd or 0.0 for a in (jev, deepseek))
    # GENRE-08: the group is read NOW, not as planned: a retag can move a track while the
    # calls are in flight. An asked track that moved was asked about its old name, so
    # nothing is stored (the answer would be read by every copy in its NEW group).
    grp = groups.load(conn)
    if grp.key(sid) != asked.ctx.group_key:
        return TrackResult(
            sid,
            OUTCOME_FAILED,
            FAIL_RETAGGED,
            cost_usd=llm_cost + search_cost,
            paid_search=asked.paid_search,
            search_cost_usd=search_cost,
        )
    # A failed call is not an answer: store no digest, so the next run asks again.
    digest = (
        None if jev.transient or deepseek.transient else request_digest(full_state(asked.ctx, asked.snippets or ""))
    )
    query = providers.search_query(asked.ctx.artist, asked.ctx.title)
    evidence = {
        "search_query": query,
        "query_version": providers.QUERY_VERSION,
        "snippets": bool(asked.snippets),
        # The group this answer is about: a later retag of the holder makes it unshared (GENRE-08).
        groups.EVIDENCE_IDENTITY: asked.ctx.group_key,
    }
    for answer in (jev, deepseek):
        findings.put_finding(
            conn,
            _finding(
                sid,
                answer,
                provider=answer.provider,
                reason=answer.reason,
                evidence=evidence,
                model=answer.model,
                request_digest=digest,
                cost_usd=answer.cost_usd,
            ),
        )
    # The group keeps ONE computed result, the one stored now; a track that left the
    # group meanwhile is no longer a copy and keeps its own findings.
    groups.delete_findings(conn, grp.siblings_of(sid), findings.COMPUTED_PROVIDERS)
    if jev.probabilities is not None:
        findings.put_vector(conn, sid, findings.PROVIDER_JEV, jev.probabilities)
    fused = providers.fuse(jev, deepseek)
    reason = None if fused.genre else f"jev: {jev.reason}; deepseek: {deepseek.reason}"
    findings.put_finding(
        conn,
        _finding(
            sid,
            fused,
            provider=findings.PROVIDER_FUSED,
            reason=reason,
            evidence={**evidence, "rule": fused.rule, "jev": jev.genre, "deepseek": deepseek.genre},
            model=f"{jev.model} + {deepseek.model}",
            request_digest=digest,
            cost_usd=round(llm_cost + search_cost, 6),
        ),
    )
    outcome = OUTCOME_DONE if fused.genre else OUTCOME_FAILED
    return TrackResult(
        sid,
        outcome,
        reason,
        fused.genre,
        fused.confidence,
        fused.rule,
        llm_cost + search_cost,
        asked.paid_search,
        search_cost,
    )


def _for_requested(conn: sqlite3.Connection, asked: _Asked, result: TrackResult) -> list[TrackResult]:
    """The group's result for each requested member: the first carries the cost, the rest share it.

    A requested member that left the group while the calls were in flight does not
    share the answer: it fails as :data:`FAIL_RETAGGED`, to be asked under its new name.
    So does every member when the copy that holds the group's result left it (an
    ``up_to_date`` group whose holder was retagged meanwhile has no result any more).
    """
    payer, *rest = asked.ctx.requested or (asked.ctx.stable_id,)
    shared = [
        replace(result, stable_id=sid, cost_usd=0.0, paid_search=False, search_cost_usd=0.0, shared_from=payer)
        for sid in rest
    ]
    grp = groups.load(conn)
    answered = (OUTCOME_DONE, OUTCOME_UP_TO_DATE)
    still = {sid for sid in (asked.ctx.stable_id, payer, *rest) if grp.key(sid) == asked.ctx.group_key}
    return [
        r
        if r.outcome not in answered or {asked.ctx.stable_id, r.stable_id} <= still
        else replace(r, outcome=OUTCOME_FAILED, reason=FAIL_RETAGGED, genre=None, confidence=None, rule=None)
        for r in (replace(result, stable_id=payer), *shared)
    ]


def _store_alone(conn: sqlite3.Connection, asked: _Asked) -> list[TrackResult]:
    """Store one group's result in its own write transaction and commit it.

    The lock is taken BEFORE the group is read again, so the membership the
    answer is checked against is the membership it is stored against.
    """
    began = groups.begin_write(conn)
    try:
        results = _for_requested(conn, asked, _store(conn, asked))
    except BaseException:
        if began:
            conn.rollback()
        raise
    conn.commit()
    return results


def _store_and_settle(
    conn: sqlite3.Connection, asked: _Asked, settle: Callable[[TrackResult], bool]
) -> list[TrackResult]:
    """Store one group and settle its requested members' jobs in one transaction.

    The store is kept when at least one member's claim still holds; when every
    one was cancelled meanwhile it is rolled back.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        results = _for_requested(conn, asked, _store(conn, asked))
        kept = [settle(result) for result in results]
        conn.execute("COMMIT" if any(kept) else "ROLLBACK")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    # Cancelled while the calls were in flight: the search was paid for, so it
    # stays cached, and the cost still counts; no finding is stored.
    if not any(kept) and asked.paid_search and asked.snippets is not None:
        findings.put_search(
            conn, providers.search_query(asked.ctx.artist, asked.ctx.title), providers.QUERY_VERSION, asked.snippets
        )
    return [
        result
        if held
        else replace(result, outcome=OUTCOME_CANCELLED, reason="cancelled while running", genre=None, confidence=None)
        for result, held in zip(results, kept, strict=True)
    ]


def required_keys(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Both keys, or :class:`providers.ProviderUnavailable` naming the first missing one."""
    return {
        "openrouter": providers.require_key(providers.OPENROUTER_KEY_ENV, environ),
        "serper": providers.require_key(providers.SERPER_KEY_ENV, environ),
    }


def _fused_digests(conn: sqlite3.Connection, contexts: Mapping[str, _Context]) -> dict[str, str | None]:
    stored = findings.findings_for(conn, contexts)
    return {
        sid: next(
            (f.request_digest for f in rows if f.provider == findings.PROVIDER_FUSED and f.request_digest is not None),
            None,
        )
        for sid, rows in stored.items()
    }


def _ask_all(
    conn: sqlite3.Connection, contexts: Mapping[str, _Context], keys: Mapping[str, str], *, force: bool, workers: int
) -> list[_Asked]:
    """Search each uncached query once, then ask the models for every track, concurrently."""
    fused_digest = _fused_digests(conn, contexts)
    query_of = {sid: providers.search_query(c.artist, c.title) for sid, c in contexts.items()}
    searched: dict[str, _Searched] = {}
    for query in dict.fromkeys(query_of.values()):
        text = findings.cached_search(conn, query, providers.QUERY_VERSION)
        if text is not None:
            searched[query] = _Searched(text, False)
    to_search = [q for q in dict.fromkeys(query_of.values()) if q not in searched]
    # The first track of each freshly searched query carries its cost; duplicates share it.
    payer = {q: next(sid for sid, qq in query_of.items() if qq == q) for q in to_search}

    def ask(c: _Context) -> _Asked:
        found = searched[query_of[c.stable_id]]
        paid_here = found.paid and payer.get(query_of[c.stable_id]) == c.stable_id
        return _ask(c, found, paid_here, keys, force, fused_digest.get(c.stable_id))

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        searched.update(zip(to_search, pool.map(lambda q: _search(q, keys["serper"]), to_search), strict=True))
        return list(pool.map(ask, contexts.values()))


# How long a run waits for another process's run on the same library before
# failing with InferenceBusy. A genre lane batch takes seconds; a CLI run of
# 500 tracks takes minutes.
CROSS_PROCESS_WAIT_S = 900.0


class InferenceBusy(RuntimeError):
    """Another run on the same library held the inference lock for longer than the wait."""


class InferenceLock:
    """Serializes inference runs on one library database.

    Overlapping runs would both see the same search-cache miss and both pay for
    it. In this process a re-entrant lock orders the threads (so the genre lane
    can take it before claiming and still call :func:`infer_tracks`); across
    processes (the engine and an operator's CLI) the outermost holder also keeps
    an EXCLUSIVE transaction open on a lock database beside the library. SQLite
    locks are portable, unlike POSIX file locks.
    """

    def __init__(self, lock_db: Path | None) -> None:
        self._local = threading.RLock()
        self._lock_db = lock_db
        self._depth = 0
        self._held: sqlite3.Connection | None = None

    def acquire(self, *, blocking: bool = True) -> bool:
        if not self._local.acquire(blocking=blocking):
            return False
        if self._depth == 0 and self._lock_db is not None:
            try:
                self._held = _hold_lock_db(self._lock_db, CROSS_PROCESS_WAIT_S if blocking else 0.0)
            except sqlite3.OperationalError as exc:
                self._local.release()
                if not blocking:
                    return False
                raise InferenceBusy(
                    f"another process held {self._lock_db} for over {CROSS_PROCESS_WAIT_S:.0f}s"
                ) from exc
        self._depth += 1
        return True

    def release(self) -> None:
        self._depth -= 1
        if self._depth == 0 and self._held is not None:
            self._held.close()  # closing ends the EXCLUSIVE transaction
            self._held = None
        self._local.release()

    def __enter__(self) -> InferenceLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _hold_lock_db(path: Path, wait_s: float) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=wait_s, isolation_level=None, check_same_thread=False)
    try:
        conn.execute("BEGIN EXCLUSIVE")
    except BaseException:
        conn.close()
        raise
    return conn


_INFERENCE_LOCKS: dict[str, InferenceLock] = {}
_INFERENCE_LOCKS_GUARD = threading.Lock()


def inference_lock(conn: sqlite3.Connection) -> InferenceLock:
    """The one :class:`InferenceLock` for ``conn``'s database file in this process."""
    row = conn.execute("PRAGMA database_list").fetchone()
    db = Path(row[2]).resolve() if row is not None and row[2] else None
    key = str(db) if db is not None else f"memory:{id(conn)}"
    with _INFERENCE_LOCKS_GUARD:
        if key not in _INFERENCE_LOCKS:
            lock_db = db.with_name(db.name + ".genre-lock") if db is not None else None
            _INFERENCE_LOCKS[key] = InferenceLock(lock_db)
        return _INFERENCE_LOCKS[key]


def infer_tracks(
    conn: sqlite3.Connection,
    stable_ids: Iterable[str],
    *,
    force: bool = False,
    keys: Mapping[str, str] | None = None,
    workers: int = DEFAULT_WORKERS,
    settle: Callable[[TrackResult], bool] | None = None,
) -> RunSummary:
    """Infer and store genres for ``stable_ids``; each group's result is committed as it is stored.

    Duplicates among ``stable_ids`` are asked once (GENRE-08): the first member
    of a group carries the result and its cost, the others get the same outcome
    with ``shared_from`` naming it and no cost.

    ``settle`` (the genre lane's queue settlement) runs inside the SAME write
    transaction as each asked track's findings; when it returns False (the job
    was cancelled meanwhile) that track's findings are rolled back, so a cancel
    can never interleave with the store. It needs an autocommit connection.

    Raises :class:`providers.ProviderUnavailable` before any call when a key is missing.
    """
    keys = dict(keys) if keys is not None else required_keys()
    providers.serper_usd_per_search()  # a malformed price fails here, before anything is paid for
    findings.ensure_schema(conn)
    ids = list(dict.fromkeys(stable_ids))
    with inference_lock(conn):
        _bind_legacy(conn)
        contexts, skipped = _plan(conn, ids)
        asked = _ask_all(conn, contexts, keys, force=force, workers=workers)
        summary = RunSummary([TrackResult(sid, OUTCOME_SKIPPED, reason) for sid, reason in skipped.items()])
        conn.commit()
        for a in asked:
            summary.results.extend(_store_alone(conn, a) if settle is None else _store_and_settle(conn, a, settle))
        # Nothing is left open: a search cached after a cancelled group is written outside its transaction.
        conn.commit()
    per_1k = summary.cost_per_1k_usd
    if per_1k is not None and per_1k > COST_CEILING_USD_PER_1K:
        log.warning("genre inference cost $%.4f per 1k tracks, over the $%.2f ceiling", per_1k, COST_CEILING_USD_PER_1K)
    log.info("genre inference: %s", {k: v for k, v in summary.as_dict().items() if k != "results"})
    return summary


# --------------------------------------------------------------- selection
def eligible_ids(conn: sqlite3.Connection, *, master_path: Path | None = None) -> list[str]:
    """Live music tracks whose audio is on this machine (the honest ``present`` denominator).

    Non-music (LIBM-178, :mod:`apps.shared.non_music`) is left out exactly as the
    Library hides it: a rekordbox sampler path, or a sample genre as tagged
    (a library edit, then rekordbox, then the imported file tag).
    """
    live = jev_store.live_track_ids(conn)
    paths = {sid: p for sid, p in bulk_local_audio_paths(conn, live).items() if p is not None}
    present = sorted(paths)
    fields = library_tags.field_genres(conn, present)
    rekordbox = library_tags.rekordbox_genres(conn, present, master_path or rb_config.MASTER_PLAIN_DB)

    def tagged(sid: str) -> str | None:
        user, imported = fields.get(sid, (None, None))
        return user or rekordbox.get(sid) or imported

    return [sid for sid in present if not is_non_music(SimpleNamespace(file_path=str(paths[sid]), genre=tagged(sid)))]


def _active_genre_jobs(conn: sqlite3.Connection) -> set[str]:
    batch_id = USER_BATCH_IDS["genre"]
    return {
        str(r[0])
        for r in conn.execute(
            "SELECT stable_id FROM analysis_queue_item WHERE batch_id = ? AND lane = 'genre' AND state IN (?, ?)",
            (batch_id, ITEM_PENDING, ITEM_RUNNING),
        )
    }


def attention_infer_ids(
    conn: sqlite3.Connection,
    stable_ids: Sequence[str],
    *,
    master_path: Path | None = None,
) -> list[str]:
    """Live ids that need a genre infer job when selected or loaded on /performance."""
    ids = list(dict.fromkeys(stable_ids))
    if not ids:
        return []
    chosen_map = display.chosen_for_stable_ids(conn, ids, master_path=master_path)
    searchable = set(searchable_ids(conn, ids))
    missing = set(missing_fused_ids(conn, ids))
    active = _active_genre_jobs(conn)
    return [
        sid for sid in ids if chosen_map.get(sid) is None and sid in searchable and sid in missing and sid not in active
    ]


def genre_attention_skip_reason(
    conn: sqlite3.Connection,
    stable_id: str,
    *,
    master_path: Path | None = None,
) -> str | None:
    """Why a genre enqueue must not run for attention, or None when it may."""
    chosen = display.chosen_for_stable_ids(conn, [stable_id], master_path=master_path).get(stable_id)
    if chosen is not None:
        return SKIP_HAS_CHOSEN_GENRE
    if stable_id in _active_genre_jobs(conn):
        return SKIP_GENRE_JOB_ACTIVE
    return None


def missing_fused_ids(conn: sqlite3.Connection, candidates: Iterable[str]) -> list[str]:
    """Candidates whose duplicate group has no complete ``odj-fused`` finding, one per group.

    Complete is ``ok`` WITH a request digest: an unknown fused finding (both
    calls failed) and a degraded one (one call failed, stored without a digest)
    do not count, so "infer everything untagged" picks the track up again.

    GENRE-08: a finding on ANY copy covers the group, and only the first
    candidate of a group is returned, so duplicates are never queued twice.
    """
    findings.ensure_schema(conn)
    _bind_legacy(conn)
    grp = groups.load(conn)
    # A finding made for a holder's OLD name covers nothing: its group is asked again.
    have = [
        sid
        for sid, rows in findings.findings_for(conn, providers=[findings.PROVIDER_FUSED]).items()
        if any(
            f.status == findings.STATUS_OK and f.request_digest is not None and groups.describes_holder(f, grp)
            for f in rows
        )
    ]
    covered = {grp.key(sid) for sid in have}
    return grp.first_per_group(sid for sid in candidates if grp.key(sid) not in covered)


__all__ = [
    "COST_CEILING_USD_PER_1K",
    "CROSS_PROCESS_WAIT_S",
    "FAIL_RETAGGED",
    "OUTCOME_CANCELLED",
    "OUTCOME_DONE",
    "OUTCOME_FAILED",
    "OUTCOME_SKIPPED",
    "OUTCOME_UP_TO_DATE",
    "SKIP_GENRE_JOB_ACTIVE",
    "SKIP_HAS_CHOSEN_GENRE",
    "SKIP_NO_ARTIST_TITLE",
    "InferenceBusy",
    "InferenceLock",
    "RunSummary",
    "TrackResult",
    "attention_infer_ids",
    "eligible_ids",
    "full_state",
    "genre_attention_skip_reason",
    "infer_tracks",
    "inference_lock",
    "missing_fused_ids",
    "request_digest",
    "required_keys",
    "searchable_ids",
]
