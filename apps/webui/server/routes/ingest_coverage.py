"""One coverage snapshot: what is done, terminal, failed and pending per step.

Split out of routes/ingest.py (which sits near its size ceiling) and shared by
two readers that must never disagree: GET /ingest/coverage, which feeds the
health lights, and the auto-drain (``apps.webui.server.coverage_drain``),
which decides what work is left. One snapshot, one set of numbers.

The denominator is ``present``: tracks whose audio resolves on THIS machine
(``apps.webui.server.library_playable``). Per step, every present track is in
exactly one of four states, and the snapshot asserts they sum to ``present``:

==========  ================================================================
state       meaning
==========  ================================================================
done        the artifact exists and validates
terminal    nothing to make: no lyrics available, or no stems source. Counts
            as finished, and is reported separately so it is never mistaken
            for done
failed      a drain job (analysis included) failed ``MAX_ATTEMPTS`` times on
            this audio file.
            Not retried until the file changes or the failure is cleared
pending     not yet tried, or failed and still inside its retry budget
==========  ================================================================

A light is green when ``pending == 0 and failed == 0``.

Stems ``done`` has two parts (HEALTH-07): ``local`` (a valid bundle on this
disk) and ``in cloud`` (no local bundle, but the R2 stem index lists one this
machine can fetch on demand; ``coverage_cloud``). An evicted bundle is done:
nothing needs rendering. When the index cannot be read the missing bundles
stay ``pending`` and ``stem_cloud.state`` is ``unknown``, so the lights that
depend on the answer render grey: unknown is never counted as done.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 the four states partition ``present`` for every step
    [if] done + terminal + failed + pending != present [then ⛔️] raise
  ✔︎ ✅ 🎯 terminal is distinguishable from not-yet-tried
    [if] a track has an ``instrumental`` lyrics verdict [then] lyrics terminal
    [if] a track has no verdict at all [then] lyrics pending, never terminal
    [if] stems have no source for a track [then] stems AND vocals terminal
  ✔︎ ✅ 🎯 vocals work is only actionable once stems exist
    [if] a track has no stem bundle and stems are pending
    [then] vocals pending and counted in ``waiting_on_stems``
  ✔︎ ✅ 🎯 HEALTH-07 an evicted bundle that R2 holds is done, not pending
    [if] no local bundle and the index lists one [then] stems done, in cloud
    [if] no local bundle and NOT in the index [then] stems pending
    [if] the index cannot be read [then] pending, cloud state unknown
    [if] vocals are missing and the bundle is in cloud
    [then] vocals pending in ``vocals_cloud_ready``, not ``waiting_on_stems``
"""
from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from apps.cloud import stem_index
from apps.lyrics import cache as lyrics_cache
from apps.lyrics import fetch_verdicts, lookup_metadata
from apps.lyrics.asr_hallucination import cached_entry_is_no_lyrics
from apps.webui.server import coverage_cloud, library_playable
from apps.webui.server import coverage_outcomes as outcomes_mod
from apps.webui.server.routes import ingest_job

STEPS: tuple[str, ...] = ("analysis", "stems", "vocals", "lyrics")
Target = tuple[str, str]


@dataclass(frozen=True)
class StepCounts:
    done: int
    terminal: int
    failed: int
    pending: int
    corrupt: int


@dataclass(frozen=True)
class CoverageSnapshot:
    playability: library_playable.LibraryPlayability
    on_disk: list[Target]
    unreachable: int
    missing: dict[str, list[Target]]
    corrupt: dict[str, list[Target]]
    counts: dict[str, StepCounts]
    #: Per step, the (stable_id, path) targets still worth an attempt.
    pending: dict[str, list[Target]]
    #: Vocals targets that have a stem bundle to derive from, in row order.
    vocals_ready: list[Target]
    #: Present tracks with no vocals because their stems are still pending.
    waiting_on_stems: int
    stems_source_refusal: str | None
    #: Stems ``done`` split: bundles on this disk, and bundles only in R2.
    stems_local: int = 0
    stems_in_cloud: int = 0
    #: Vocals targets whose bundle must be fetched from R2 before deriving.
    vocals_cloud_ready: list[Target] = field(default_factory=list)
    stem_cloud: coverage_cloud.StemCloud = coverage_cloud.OFF
    generated_at: float = field(default_factory=time.time)

    @property
    def green(self) -> bool:
        return all(
            self.counts[step].pending == 0 and self.counts[step].failed == 0
            for step in ("stems", "vocals", "lyrics")
        )


#-----------------------------------------------------------------------------
# terminal lyrics verdicts
#-----------------------------------------------------------------------------
def default_stems_source_refusal() -> str | None:
    """Why NO executor can make stems on this install, or None when one can.

    ``local_stems_gate`` answers None whenever the remote farm is reachable or
    the on-device executor is allowed, so a non-None answer means a missing
    stem bundle has no source here at all.
    """
    from apps.stems.local_gate import local_stems_gate

    return local_stems_gate()


def lyrics_terminal_ids(
    data_dir: Path,
    candidates: Sequence[str],
    index: Mapping[str, Mapping[str, str]] | None = None,
) -> set[str]:
    """Ids with no lyrics to fetch: a final fetch verdict, or a no-lyrics ASR cache.

    Reads the store ``LyricsFetchService`` writes, through its own freshness
    rule: ``instrumental`` is final, ``no_source`` is final only while the
    track's vocals stem is the one the verdict was recorded against.
    """
    if not candidates:
        return set()
    # LYRICS-12: a cached ASR transcript that is only hallucinations is the
    # same final answer as an ``instrumental`` verdict, whatever its ledger says.
    # One directory listing, so only ids that HAVE a cache file are parsed.
    lyrics_dir = lyrics_cache.cache_dir(data_dir)
    cached = {p.stem for p in lyrics_dir.glob("*.json")} if lyrics_dir.is_dir() else set()
    terminal: set[str] = {
        sid for sid in candidates if sid in cached and cached_entry_is_no_lyrics(data_dir, sid)
    }
    verdict_dir = fetch_verdicts.verdict_dir(data_dir)
    if not verdict_dir.is_dir():
        return terminal
    if index is None:
        index = stem_index.load_cached_index_memo(data_dir)
    for stable_id in candidates:
        if stable_id in terminal:
            continue
        verdict = fetch_verdicts.load_verdict(data_dir, stable_id)
        if verdict is None or verdict.outcome == "cached":
            # ``cached`` with no lyrics-cache entry means the entry was lost;
            # the verdict proves nothing about there being no lyrics.
            continue
        vocals_sha256 = next(
            (digest for name, digest in (index.get(stable_id) or {}).items() if name.startswith("vocals.")),
            None,
        )
        if fetch_verdicts.is_terminal_fresh(verdict, vocals_sha256):
            terminal.add(stable_id)
    return terminal


#-----------------------------------------------------------------------------
# the snapshot
#-----------------------------------------------------------------------------
def _partition(
    step: str,
    present: list[Target],
    done: set[str],
    terminal: set[str],
    failed: set[str],
    corrupt: int,
) -> tuple[StepCounts, list[Target]]:
    pending = [
        (sid, path)
        for sid, path in present
        if sid not in done and sid not in terminal and sid not in failed
    ]
    present_ids = {sid for sid, _path in present}
    counts = StepCounts(
        done=len(done & present_ids),
        terminal=len((terminal - done) & present_ids),
        failed=len((failed - done - terminal) & present_ids),
        pending=len(pending),
        corrupt=corrupt,
    )
    total = counts.done + counts.terminal + counts.failed + counts.pending
    if total != len(present):
        raise RuntimeError(
            f"{step} coverage states sum to {total}, not the {len(present)} present tracks"
        )
    return counts, pending


def _by_bundle_place(
    targets: Sequence[Target], local: set[str], in_cloud: set[str]
) -> tuple[list[Target], list[Target]]:
    """(targets whose stem bundle is on disk, targets whose bundle is in R2)."""
    return (
        [target for target in targets if target[0] in local],
        [target for target in targets if target[0] in in_cloud],
    )


def split_stems_by_place(
    missing_stems: Sequence[Target], stem_cloud: coverage_cloud.StemCloud
) -> tuple[list[Target], list[Target]]:
    """(to make, in cloud) for tracks with no local bundle.

    The ONE place "no local bundle" is split into "nothing has it" and
    "R2 has it and this machine can fetch it". Coverage, the drain (through
    the snapshot) and the refresh job all use it, so they cannot disagree
    about which stems are left to make.
    """
    to_make: list[Target] = []
    in_cloud: list[Target] = []
    for target in missing_stems:
        (in_cloud if stem_cloud.holds(target[0]) else to_make).append(target)
    return to_make, in_cloud


def _ids_without_lookup_metadata(
    conn_factory: Callable[[], sqlite3.Connection], stable_ids: Sequence[str]
) -> set[str]:
    conn = conn_factory()
    try:
        return lookup_metadata.ids_without_lookup_metadata(conn, stable_ids)
    finally:
        conn.close()


def _audio_no_source(outcome: outcomes_mod.Outcome | None, signature: str) -> bool:
    """A ledger no_source that is about the audio, not the row's metadata."""
    return outcomes_mod.is_no_source(outcome, signature) and not (
        outcome is not None and lookup_metadata.is_metadata_reason(outcome.reason)
    )


def compute_snapshot(
    conn_factory: Callable[[], sqlite3.Connection],
    stem_roots: Sequence[Path],
    vocal_dir: Path,
    lyrics_dir: Path,
    data_dir: Path,
    *,
    stems_source_refusal: str | None,
    stem_cloud: coverage_cloud.StemCloud = coverage_cloud.OFF,
) -> CoverageSnapshot:
    scan = ingest_job.playability(conn_factory)
    present = list(scan.present)
    unreachable = len(scan.broken_here) + scan.off_machine + scan.awaiting_volume + scan.pathless
    missing, corrupt = ingest_job.missing_by_step(
        present, conn_factory, stem_roots, vocal_dir, lyrics_dir
    )
    missing_ids = {step: {sid for sid, _path in targets} for step, targets in missing.items()}
    present_ids = {sid for sid, _path in present}
    done = {step: present_ids - missing_ids[step] for step in STEPS}
    stems_local = done["stems"]
    # Evicted, not missing: R2 holds the bundle and this machine can fetch it.
    stems_in_cloud = {sid for sid, _path in split_stems_by_place(missing["stems"], stem_cloud)[1]}
    done["stems"] = stems_local | stems_in_cloud

    ledger = outcomes_mod.OutcomeStore(outcomes_mod.store_path(data_dir)).load()
    token: dict[str, str] = {}

    def _token(stable_id: str, path: str) -> str:
        if stable_id not in token:
            token[stable_id] = outcomes_mod.audio_token(Path(path))
        return token[stable_id]

    def _ledger_ids(step: str, predicate: Callable[[outcomes_mod.Outcome | None, str], bool]) -> set[str]:
        return {
            sid
            for sid, path in missing[step]
            if (step, sid) in ledger and predicate(ledger[(step, sid)], _token(sid, path))
        }

    stems_terminal = (
        set(missing_ids["stems"]) - stems_in_cloud
        if stems_source_refusal is not None
        else _ledger_ids("stems", outcomes_mod.is_no_source)
    )
    # Vocals are derived from stems: no stems source means no vocals source.
    vocals_terminal = (stems_terminal & missing_ids["vocals"]) | _ledger_ids(
        "vocals", outcomes_mod.is_no_source
    )
    # An unreadable index proves no vocals digest, so a ``no_source`` verdict
    # recorded against one stays pending rather than failing the whole read.
    lyrics_candidates = [sid for sid, _path in missing["lyrics"]]
    lyrics_terminal = (
        lyrics_terminal_ids(data_dir, lyrics_candidates, {} if stem_cloud.state == "unknown" else None)
        # A row with no artist, title or duration is terminal only while it
        # lacks them, measured now: a ledger entry saying so was keyed on the
        # audio file and outlived the tag re-read that fixed the row (all
        # 1,274 rows on demon-llama, Thu 1 Oct 2026).
        | _ids_without_lookup_metadata(conn_factory, lyrics_candidates)
        | _ledger_ids("lyrics", _audio_no_source)
    )
    terminal = {
        "analysis": _ledger_ids("analysis", outcomes_mod.is_no_source),
        "stems": stems_terminal,
        "vocals": vocals_terminal,
        "lyrics": lyrics_terminal,
    }
    failed = {step: _ledger_ids(step, outcomes_mod.is_failed_terminal) for step in STEPS}

    counts: dict[str, StepCounts] = {}
    pending: dict[str, list[Target]] = {}
    for step in STEPS:
        counts[step], pending[step] = _partition(
            step, present, done[step], terminal[step], failed[step], len(corrupt[step])
        )
    vocals_ready, vocals_cloud_ready = _by_bundle_place(
        pending["vocals"], stems_local, stems_in_cloud
    )
    return CoverageSnapshot(
        playability=scan,
        on_disk=present,
        unreachable=unreachable,
        missing=missing,
        corrupt=corrupt,
        counts=counts,
        pending=pending,
        vocals_ready=vocals_ready,
        waiting_on_stems=len(pending["vocals"]) - len(vocals_ready) - len(vocals_cloud_ready),
        stems_source_refusal=stems_source_refusal,
        stems_local=len(stems_local),
        stems_in_cloud=len(stems_in_cloud),
        vocals_cloud_ready=vocals_cloud_ready,
        stem_cloud=stem_cloud,
    )


def response_fields(snapshot: CoverageSnapshot) -> dict[str, object]:
    """The JSON-ready body of GET /ingest/coverage."""
    counts = snapshot.counts
    return {
        "on_disk": len(snapshot.on_disk),
        "unreachable": snapshot.unreachable,
        "missing": {step: len(targets) for step, targets in snapshot.missing.items()},
        "corrupt": {step: len(targets) for step, targets in snapshot.corrupt.items()},
        "availability": snapshot.playability.counts(),
        "done": {step: counts[step].done for step in STEPS},
        "terminal": {step: counts[step].terminal for step in STEPS},
        "failed": {step: counts[step].failed for step in STEPS},
        "pending": {step: counts[step].pending for step in STEPS},
        "waiting_on_stems": snapshot.waiting_on_stems,
        "local": {"stems": snapshot.stems_local},
        "in_cloud": {"stems": snapshot.stems_in_cloud},
        "awaiting_stem_download": len(snapshot.vocals_cloud_ready),
        "stems_index": snapshot.stem_cloud.as_dict(),
        "stems_source_refusal": snapshot.stems_source_refusal,
        "generated_at": snapshot.generated_at,
    }


__all__ = [
    "STEPS",
    "CoverageSnapshot",
    "StepCounts",
    "compute_snapshot",
    "default_stems_source_refusal",
    "lyrics_terminal_ids",
    "response_fields",
    "split_stems_by_place",
]
