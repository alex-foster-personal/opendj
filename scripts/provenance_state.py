"""Durable local state for the prompt provenance sweep.

The gitignored, per-checkout side of the sweep: the freshness watermark, the
lock that keeps overlapping runs from clobbering each other, and the atomic
replace every tracked write goes through. Separate from
:mod:`scripts.provenance_sweep` because this is the part that is NOT versioned
with the repo, and the mismatch between it and the tracked ledger is exactly
what caused the loss the sweep now guards against.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import stat
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from scripts.provenance_probe import generation_conflict

# Advisory whole-file locking, which POSIX and Windows spell differently and
# neither of which is importable on the other. Imported at module scope, and
# branched on explicitly, because `import fcntl` alone raised ModuleNotFoundError
# on every Windows checkout - and `.claude/settings.json` runs this module from
# the Stop hook, so the sweep died before argument parsing on bifrost2 rather
# than reporting anything. Codex found it on #708.
if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

OUT_DIRNAME = "docs/threads"
LEDGER = "prompts.jsonl"
WATERMARK = ".provenance-watermark.json"
LOCKFILE = ".provenance-lock"


class LockUnavailable(RuntimeError):
    """Locking itself failed here, as distinct from another sweep holding it.

    A dedicated type rather than the raw OSError so the caller's handler can be
    narrowed to the ACQUISITION and nothing else. Catching OSError around the
    whole `with` block caught every downstream OSError the harvest could raise -
    a missing `gh`, a full disk - and reported them as "cannot lock", which
    routed a real failure past the error recorder under a false name. Codex
    found that on #708, in the round after the contention fix that created it.
    Always raised `from` the original, so the errno survives for a reader.
    """


def _write_atomic(path: Path, text: str) -> None:
    """Replace a file in one step, so no reader ever sees a half-written ledger.

    The Stop hook backgrounds a sweep that takes minutes, so a reader (or the
    next sweep) can arrive mid-write. os.replace is atomic within a filesystem
    and the temp file is created alongside the target to stay on one.

    The temp name is UNIQUE PER WRITER, which the fixed `<name>.tmp` was not.
    Two detached hook runs that both refuse an unavailable source call
    `record_error` before either takes the lock, so both wrote that one path:
    the second could replace the first's temp file under it, leaving one
    process to publish the other's bytes or to fail its own replace outright.
    A shared scratch file makes the replace atomic and the WRITE racy, which is
    the same defect one layer down. Codex found it on #708.

    `mkstemp` creates with O_EXCL, so uniqueness is the filesystem's answer
    rather than a name this process picked and hoped was free. The cleanup only
    matters on the failure path: after a successful `os.replace` there is
    nothing left at the temp name to remove.
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        # MODE, restored before the replace. mkstemp creates 0600 and
        # os.replace carries the SOURCE inode's mode to the destination, so
        # the mkstemp fix above would have turned the tracked ledger and index
        # owner-only on the first changing sweep, unreadable to any other user
        # or service until a checkout recreated them. Codex found it on #708.
        # An existing file keeps the mode it already had; a new one gets the
        # 0644 that git checks these out with, stated rather than inherited
        # from whatever umask the calling process happens to hold.
        os.chmod(tmp, stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def sweep_lock(repo: Path):
    """Hold an exclusive per-checkout lock, or yield False if one is already held.

    Every Stop detaches a sweep, and a full sweep takes minutes, so turns overlap
    routinely. Two processes that both read the ledger before either writes would
    each write their own union, and the slower one silently drops whatever the
    faster one had just harvested - the same loss this tool exists to prevent,
    arriving by a different door. A skipped run costs nothing: the next sweep
    reads the same sources.
    """
    path = repo / OUT_DIRNAME / LOCKFILE
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = path.open("w")
    except OSError as exc:
        # Not being able to CREATE the lock file is the same class of thing as
        # not being able to lock it: nobody else holds this checkout, so
        # skipping would disable the sweep here silently and forever. Raised as
        # the same type so the caller has one thing to catch.
        raise LockUnavailable(f"cannot open {path}: {exc}") from exc
    try:
        # CONTENTION IS NOT THE SAME AS "LOCKING DOES NOT WORK HERE", and this
        # used to catch a bare OSError, so it read both as "another sweep owns
        # the checkout" - a clean skip, exit 0, no harvest, no watermark, no
        # error. On a filesystem that refuses advisory locks (ENOLCK on some
        # network mounts, EINVAL, an I/O error) that is not a skip, it is the
        # Stop hook silently disabled forever on that checkout, because every
        # subsequent run takes the same branch. The Stop hook sends stderr to
        # /dev/null, so nothing would ever have surfaced it. Codex found it on
        # #708; it is the third instance of one class on this PR - an
        # unmeasured source rendered as a measurement - after the absent named
        # store and the unreadable one.
        try:
            _lock_exclusive_nonblocking(handle)
        except OSError as exc:
            if not _is_contention(exc):
                raise LockUnavailable(f"cannot lock {path}: {exc}") from exc
            yield False
            return
        yield True
    finally:
        handle.close()


def _is_contention(exc: OSError) -> bool:
    """Does this OSError mean "someone else holds the lock", or something worse?

    The two spellings report contention differently and neither is a single
    errno, so the test is a small allowed SET and everything outside it is a
    failure that must be raised rather than absorbed:

    - POSIX `flock` with LOCK_NB raises EWOULDBLOCK, which on Linux and macOS
      is the same value as EAGAIN; both names are listed because that equality
      is not guaranteed by POSIX.
    - Windows `msvcrt.locking` with LK_NBLCK raises EACCES for a region another
      process holds, and EDEADLOCK for the blocking variants. EACCES is not
      listed for POSIX flock, where it would mean a permission problem, hence
      the platform branch.

    Kept as a predicate over the exception rather than inline in the handler so
    the discrimination can be driven with real errnos in a test. That is the
    same move as typing `_within` on PurePath: the failure modes worth checking
    belong to filesystems this machine cannot produce on demand, so the rule is
    made addressable instead of reported UNAVAILABLE.
    """
    if sys.platform == "win32":
        return exc.errno in {errno.EACCES, errno.EDEADLOCK}
    return exc.errno in {errno.EWOULDBLOCK, errno.EAGAIN}


def _lock_exclusive_nonblocking(handle) -> None:
    """Take an exclusive advisory lock on `handle`, or raise OSError.

    Two spellings of one primitive. Both raise OSError when the lock is already
    held, which is what the caller reads as "another sweep owns this checkout";
    neither blocks. The lock is released by closing the handle on both
    platforms, so the caller's `finally` is the only unlock path.

    msvcrt.locking needs a byte RANGE and a file position, hence the seek and
    the one-byte region: Windows has no whole-file form, and one byte at offset
    0 is the conventional stand-in because every contender asks for the same
    byte.
    """
    if sys.platform == "win32":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _fingerprint(ledger: Path) -> tuple[int, str]:
    if not ledger.exists():
        return 0, ""
    raw = ledger.read_bytes()
    return raw.count(b"\n"), hashlib.sha256(raw).hexdigest()[:16]


def read_banked_cutoff(repo: Path, sources_id: str) -> float | None:
    """The cutoff a previous sweep for THIS source set recorded, or None.

    Exposed because a store's GENERATION is only meaningful relative to a
    reference time: "how many files under this root are older than the cutoff
    the last sweep banked" is a question a caller can only ask once it knows
    that cutoff, and this module is the one that holds it. Returning it is
    cheaper and clearer than teaching this module what a source is - the same
    reason `sources_id` arrives as an opaque token.
    """
    wm_path = repo / OUT_DIRNAME / WATERMARK
    if not wm_path.exists():
        return None
    try:
        wm = json.loads(wm_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if wm.get("sources_id") != sources_id:
        return None
    swept = wm.get("swept_at")
    return float(swept) if isinstance(swept, (int, float)) else None


def _source_claim_defect(wm: dict, sources_id: str, generations: dict[str, str]) -> str | None:
    """Why this watermark's claim about WHICH stores it covers does not hold.

    Extracted from `read_watermark` because it is a second question wearing the
    same shape as the first: the fingerprint half asks whether the LEDGER the
    watermark was written against is the one on disk, and this half asks
    whether the STORES are. Returns a reason, or None when the claim stands.

    Three ways it fails, in widening order. The watermark may predate the field
    entirely, in which case it cannot answer and fails open. It may name a
    different source set. Or it may name this one and cover a different
    GENERATION of it - the case a name is blind to, and the reason `generations`
    exists.
    """
    if wm.get("sources_id") is None:
        return "watermark predates source tracking: full harvest"
    if wm["sources_id"] != sources_id:
        return "watermark covers different sources: full harvest"
    recorded = wm.get("sources_generation")
    if not isinstance(recorded, dict):
        return "watermark predates source generations: full harvest"
    # Only where BOTH runs saw the store. A name in one and not the other is
    # the appearance or disappearance case, which the CLI already answers, and
    # reporting it here too would name one condition twice in two vocabularies.
    # CONTAINMENT, not equality: `generations` measures only the files older
    # than the banked cutoff, and `recorded` banked every file in the store.
    # Comparing them as equals made an open session's next turn read as a
    # replaced source. See `generation_conflict`.
    replaced = sorted(
        name
        for name in recorded.keys() & generations.keys()
        if generation_conflict(recorded[name], generations[name])
    )
    if replaced:
        return (
            f"source(s) are not the generation this watermark covers "
            f"({', '.join(replaced)}): full harvest"
        )
    return None


def read_watermark(repo: Path, sources_id: str, generations: dict[str, str]) -> tuple[float, str]:
    """(since, reason). `since` is 0 whenever the checkout cannot be trusted.

    The watermark says "every source older than T is already recorded". That
    claim is only true of the ledger the watermark was WRITTEN against. A branch
    cut before a sweep commit, or a rebase that dropped one, leaves a fresh
    watermark sitting on a ledger that never received those rows, and the mtime
    filter then hides their sources forever. Fingerprinting the ledger turns
    that silent, permanent loss into a one-off full re-harvest.

    It is equally a claim about WHICH SOURCES were read, and that half only
    became falsifiable when the store roots turned into flags: a run against an
    empty `--claude-root` recorded nothing and still advanced this one shared
    cutoff, so the next default-root run honoured it and skipped every real
    transcript older than it, permanently and quietly, because both runs
    succeeded. Codex found it on #708, one round after the flags landed. The
    `sources_id` check makes that self-healing exactly as the fingerprint does:
    a watermark whose sources are not this run's is not honoured, and the run
    re-harvests in full. A one-off cost, not a loss.

    `generations` closes the same claim's LAST hole, and the one a name cannot
    see. The stores can be the same set, at the same paths, and still not be
    the same stores: a backup restore recreates a transcript directory at its
    configured path with the old files' mtimes intact, so `sources_present` is
    unchanged, `sources_id` is unchanged, the ledger fingerprint is unchanged,
    and this run honours a cutoff written against a generation that is gone.
    Every restored file predates it and is skipped for good. The mid-run
    identity probe in the CLI cannot reach this: it compares one invocation's
    two snapshots, and the replacement happened while nothing was running.

    A generation covers TWO events, and neither is visible to a name: the whole
    store swapped at its path, and history arriving inside a store that kept its
    identity. The caller decides what a generation is made of; this function
    only asks whether it is the one the watermark covers.

    A cross-run change RE-SCANS; it does not refuse. That asymmetry is the
    point. Mid-harvest, a replacement means this run READ a generation it
    cannot vouch for, so banking would be a lie. Between runs it means only
    that the cutoff no longer applies, which a full harvest fixes and a refusal
    would not - a machine whose store is legitimately replaced would then never
    sweep again. Codex found it on #708, one round after the mid-run half.
    """
    wm_path = repo / OUT_DIRNAME / WATERMARK
    if not wm_path.exists():
        return 0.0, "no watermark: full harvest"
    try:
        wm = json.loads(wm_path.read_text())
        since = float(wm.get("swept_at", 0))
    except (json.JSONDecodeError, ValueError, TypeError, OSError):
        return 0.0, "unreadable watermark: full harvest"
    rows, digest = _fingerprint(repo / OUT_DIRNAME / LEDGER)
    if wm.get("ledger_rows") is None or wm.get("ledger_digest") is None:
        return 0.0, "watermark predates fingerprinting: full harvest"
    stale = _source_claim_defect(wm, sources_id, generations)
    if stale is not None:
        return 0.0, stale
    if rows < int(wm["ledger_rows"]) or digest != wm["ledger_digest"]:
        return 0.0, (
            f"ledger moved under the watermark ({wm['ledger_rows']} rows -> {rows}): "
            "checkout is stale, full harvest"
        )
    return since, "incremental"


def write_watermark(
    repo: Path,
    cutoff: float,
    sources_id: str,
    present: set[str],
    generations: dict[str, str],
) -> None:
    """Persist the cutoff CAPTURED BEFORE harvesting, never `now`.

    A sweep reads its sources over minutes. Stamping the finish time claims
    everything older than it is recorded, which is false for any transcript
    written after its harvester walked past but before the stamp: that source's
    mtime lands under the watermark, every later incremental sweep skips it, and
    the fingerprint still matches because the ledger was written without it. The
    row is then unreachable forever. Same defect as the worktree scoping bug,
    reached by a narrower window.

    `sources_id` is an opaque token from the caller, deliberately: this module
    knows about durability and nothing about what a source IS, so the identity
    of the stores is computed where they are defined and only stored here.
    `present` is the same kind of token - the names of the stores this run
    actually found - recorded so a LATER run can tell a store that vanished
    from one that was never installed. See `read_sources_present`.

    `generations` is that token sharpened from a NAME to an IDENTITY, for the
    one case a name is blind to: a store replaced at the same path between two
    sweeps. Recorded per store rather than folded into `sources_id` so the
    reader can say WHICH store changed, and so a replacement re-scans instead
    of invalidating the whole watermark's provenance. See `read_watermark`.
    """
    rows, digest = _fingerprint(repo / OUT_DIRNAME / LEDGER)
    _write_atomic(
        repo / OUT_DIRNAME / WATERMARK,
        json.dumps(
            {
                "swept_at": cutoff,
                "sources_id": sources_id,
                "sources_present": sorted(present),
                "sources_generation": dict(sorted(generations.items())),
                "ledger_rows": rows,
                "ledger_digest": digest,
            },
            indent=2,
        ),
    )


def read_sources_present(repo: Path, sources_id: str) -> set[str] | None:
    """Which stores a previous sweep for THIS source set actually found on disk.

    Absence has two meanings and only one of them is a measurement. A machine
    that has never run Codex has no Codex store, and harvesting nothing from it
    is TRUE. A store that was there last sweep and is not there now - an
    unmounted home, a restore still in progress - is UNMEASURED, and the sweep
    that treats it as empty banks a watermark over sources it never read. When
    the store comes back, every transcript older than that cutoff is skipped
    for good. That is the same permanent silent loss the watermark scoping and
    the unreadable-store refusal each close by a different door; this is the
    door where the store simply is not there. Codex found it on #708.

    Scoped to `sources_id` for the same reason the cutoff is: a record of what
    the DEFAULT stores looked like says nothing about a run pointed elsewhere,
    and honouring it across source sets would refuse runs that are correct.
    Returns None when there is NO durable record to compare against, and a set
    - possibly empty - when there is. The two were one value here, and an empty
    set meant both. That is the same conflation this whole function exists to
    undo, one level up: a sweep that legitimately found none of the three
    stores records `sources_present: []`, which is a MEASUREMENT, and reading
    it back as "no prior state" makes every store that later appears invisible
    to the appearance check. Its files older than the banked cutoff are then
    skipped for good. Codex found it on #708, one round after the appearance
    check itself landed.
    """
    wm_path = repo / OUT_DIRNAME / WATERMARK
    if not wm_path.exists():
        return None
    try:
        wm = json.loads(wm_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if wm.get("sources_id") != sources_id:
        return None
    seen = wm.get("sources_present")
    return set(seen) if isinstance(seen, list) else None


def record_error(repo: Path, message: str) -> None:
    """Leave a readable trace of a refused write, without advancing freshness.

    The Stop hook sends stdout and stderr to /dev/null, so an abort that only
    printed would make the sweep a silent no-op forever -- the same class of
    invisible failure this whole change exists to remove. `--stats` reads this
    back, and the watermark is deliberately NOT advanced, so the next run
    retries rather than treating the skipped work as done.
    """
    wm_path = repo / OUT_DIRNAME / WATERMARK
    try:
        wm = json.loads(wm_path.read_text()) if wm_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        wm = {}
    wm["last_error"] = message
    wm["last_error_at"] = datetime.now(UTC).isoformat()
    wm_path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(wm_path, json.dumps(wm, indent=2))
