"""The command-line front end of the prompt provenance sweep.

Split out of :mod:`scripts.provenance_sweep` when that module crossed the
600-line limit for the second time, on the seam the TESTS had already been
split on: `tests/scripts/test_provenance_cli.py` starts a subprocess and
asserts on exit codes and stderr, while `test_provenance_sweep.py` calls
functions in-process. This is that same line drawn through the production
code - argument parsing, the refusal protocol and the stats report on one
side, the harvest and the write guard on the other.

This module owns the ENTRY POINT. `scripts.provenance_sweep` no longer has a
`__main__` block, because a module that imported this one back to get it would
be loaded twice under `python -m` and quietly hand out two copies of itself.

    uv run --no-project python -m scripts.provenance_cli --repo <path>

Exit codes are the whole interface: `.claude/settings.json` runs this from the
Stop hook with stdout and stderr sent to /dev/null, so 0 clean, 1 bad repo,
2 regression refused, 3 UNAVAILABLE is all a caller can observe.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

try:
    from scripts.feedback_prompts_export import exportable_rows, write_export
except ModuleNotFoundError as exc:
    if exc.name == "scripts":
        raise SystemExit("uv run --no-sync python -m scripts.provenance_cli") from None
    raise
from scripts.provenance_links import GhUnavailable, attach_links, branch_windows, pr_map
from scripts.provenance_probe import (
    Probe,
    _store_generations,
    _unreadable_stores,
    generation_conflict,
)
from scripts.provenance_sources import SourceRoots, harvest_all
from scripts.provenance_state import (
    OUT_DIRNAME,
    WATERMARK,
    LockUnavailable,
    read_banked_cutoff,
    read_sources_present,
    read_watermark,
    record_error,
    sweep_lock,
    write_watermark,
)
from scripts.provenance_sweep import INDEX_PATH, RegressionError, committed_rows, write_out


def _refuse(repo: Path, why: str, *, record: bool) -> int:
    """Report a refusal, optionally persisting it, and return the exit code.

    Printing alone made these paths invisible. `.claude/settings.json` runs the
    sweep from the Stop hook detached with both streams redirected, so the
    shell sees a successful launch and never observes the child's output or
    exit status - every turn could stop harvesting in silence, and `--stats`,
    whose whole job is to report the last refused write, had nothing to report
    because only `RegressionError` ever reached `record_error`. Codex found
    that on #708, after the refusals it had asked for were added without it.

    `record_error` deliberately does NOT advance `swept_at`, so persisting the
    reason costs no freshness: the next run still retries the same window.

    `record` is False for `--stats`, whose contract is "report only, write
    nothing". A diagnostic that mutates the state it exists to describe is
    the wrong shape twice over: it makes reading the sweep's health an action
    with a side effect, and it lets a `--stats` run overwrite the very
    `last_error` a human ran it to read. The refusal is still REPORTED on
    stderr and still exits 3, so nothing is hidden - only the write is
    dropped, and the next real run records it anyway. Codex found it on #708.
    """
    if record:
        record_error(repo, why)
    print(f"[UNAVAILABLE] {why}", file=sys.stderr)
    return 3


def _export_feedback_pins(repo: Path, feedback_dir: Path) -> int:
    """Publish the validated pin projection after a successful prompt sweep."""
    try:
        path = write_export(repo, feedback_dir)
    except (LockUnavailable, ValueError, TypeError, OSError) as exc:
        return _refuse(repo, f"configured feedback source failed: {exc}", record=True)
    print(f"[OK] feedback pins -> {path}")
    return 0


def _finish_with_pin_export(repo: Path, feedback_dir: Path | None) -> int:
    """Export pins when configured without making their local source a prompt-sweep prerequisite."""
    if feedback_dir is None:
        message = (
            "pin export UNAVAILABLE: no --feedback-dir was configured; prompt provenance "
            "completed, but no user-prompt pin ledger was written"
        )
        record_error(repo, message)
        print(f"[UNAVAILABLE] {message}", file=sys.stderr)
        return 0
    return _export_feedback_pins(repo, feedback_dir)


def _report_stats(repo: Path, prompts: list[dict], since: float, why: str) -> int:
    """Print what a sweep WOULD do, and what the last one refused to do.

    The last refusal is read back out of the watermark file rather than a log,
    because the Stop hook sends both streams to /dev/null: the file is the only
    channel a human has. Lifted out of `main` when it crossed the complexity
    ceiling; it is a self-contained report and `main` is control flow.
    """
    by: dict[str, int] = {}
    for prompt in prompts:
        by[prompt["tool"]] = by.get(prompt["tool"], 0) + 1
    prior = committed_rows(repo)
    print(
        f"[stats] {why} (since={since:.0f}) new prompts: {by or 'none'}; "
        f"{len(prior)} rows guarded from HEAD/trunk"
    )
    wm_path = repo / OUT_DIRNAME / WATERMARK
    if wm_path.exists():
        try:
            last = json.loads(wm_path.read_text()).get("last_error")
        except (json.JSONDecodeError, OSError):
            last = None
        if last:
            print(f"[stats] last refused write: {last}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument(
        "--feedback-dir", type=Path, default=None,
        help="feedback directory containing comments.json and archive-*.json",
    )
    ap.add_argument("--full", action="store_true", help="ignore the watermark and re-harvest")
    ap.add_argument("--stats", action="store_true", help="report only, write nothing")
    # Where to harvest FROM. Defaulting to the real per-user stores means no
    # existing caller changes; naming them is how a caller points the sweep at
    # a different set of stores, the same way --data-dir points the app at a
    # different state dir. Added so the regression suite can drive this CLI
    # over stores it laid down, rather than reassigning module globals
    # (Codex, #708).
    ap.add_argument(
        "--claude-root",
        type=Path,
        default=None,
        help="Claude Code projects dir (default: ~/.claude/projects)",
    )
    ap.add_argument(
        "--codex-root",
        type=Path,
        default=None,
        help="Codex sessions dir (default: ~/.codex/sessions)",
    )
    ap.add_argument(
        "--cursor-db",
        type=Path,
        default=None,
        help="Cursor state.vscdb (default: the per-user globalStorage db)",
    )
    args = ap.parse_args()

    default_roots = SourceRoots()
    roots = SourceRoots(
        claude=args.claude_root or default_roots.claude,
        codex=args.codex_root or default_roots.codex,
        cursor_db=args.cursor_db or default_roots.cursor_db,
    )

    repo = args.repo.resolve()
    if not (repo / ".git").exists():
        print(f"[ERROR] not a git repo: {repo}", file=sys.stderr)
        return 1

    if args.feedback_dir is not None:
        try:
            exportable_rows(args.feedback_dir)
        except ValueError as exc:
            return _refuse(
                repo,
                f"configured feedback source cannot be exported: {exc}",
                record=not args.stats,
            )

    # Refusing before the watermark is read, so the refusal cannot move it.
    # Resolved AFTER `repo`, because `_refuse` needs somewhere to persist to.
    sources_id = roots.identity()
    was_present = read_sources_present(repo, sources_id)
    named = (args.claude_root, args.codex_root, args.cursor_db)
    defects, present, identities = _unreadable_stores(roots, named, was_present)
    if defects:
        return _refuse(
            repo,
            "configured source(s) cannot be read: "
            + "; ".join(defects)
            + ". Refusing to harvest: an unreadable store is unmeasured, not "
            "empty, and treating it as empty would advance the watermark past "
            "sources this run never read.",
            record=not args.stats,
        )

    # A store that was ABSENT last time and is readable now is a source this
    # watermark has never covered, and an incremental cutoff would skip every
    # file in it older than that cutoff - which is all of them, for a store
    # restored from a backup or synced down from another machine. The identity
    # token cannot see this: it hashes the configured ROOTS, which did not
    # change, so the run inherits a cutoff written when that store did not
    # exist and then records it as present, making the loss permanent unless
    # somebody thinks to pass --full. Codex found it on #708.
    #
    # Only NEWLY present stores force the full scan, not any difference: a
    # store that DISAPPEARED is refused above, and a set that merely reordered
    # is the same set.
    #
    # `is not None`, not truthiness. A previous sweep that legitimately found
    # NONE of the three stores records `sources_present: []`, and reading that
    # measurement as "no prior state" suppresses this check in exactly the case
    # it is for: the machine had nothing, a store then arrives carrying files
    # older than the banked cutoff, and every one of them is skipped for good.
    # Codex found it on #708, one round after this check landed.
    appeared = present - was_present if was_present is not None else set()

    # The watermark is scoped to the stores it was written against, so a run
    # with a source flag cannot silently vouch for the default ones.
    if args.full:
        since, why = 0.0, "--full"
    elif appeared:
        names = ", ".join(sorted(appeared))
        since, why = 0.0, f"full harvest: {names} appeared since the last sweep"
    else:
        # NOT `identities`: those are the mid-run probe's device:inode pairs,
        # which cannot see history arriving inside a store that kept its inode.
        # Computed against the BANKED cutoff so both sides of the comparison
        # count the same population. See `_store_generations`.
        since, why = read_watermark(
            repo,
            sources_id,
            _store_generations(roots, present, read_banked_cutoff(repo, sources_id)),
        )

    # Captured BEFORE the first source is read. See write_watermark.
    cutoff = datetime.now(UTC).timestamp()

    if not args.stats:
        # `sweep_lock` yields False only for genuine contention and raises
        # `LockUnavailable` for everything else, so a checkout where advisory
        # locking does not work reports UNAVAILABLE instead of masquerading as
        # "another sweep has this one". Caught rather than left to traceback
        # because the Stop hook discards both streams: the exit code is the
        # only channel, and this makes it the same 3 the unreadable-store
        # refusal uses.
        #
        # The handler catches that ONE type, not OSError, even though the
        # `with` body it wraps is the whole harvest. An OSError raised in
        # there - a missing `gh`, a full disk - is a real failure that belongs
        # to `_sweep`'s own reporting, and labelling it "cannot lock" both
        # misnames it and routes it past `record_error`. Codex found that on
        # #708, one round after the contention fix introduced it.
        try:
            with sweep_lock(repo) as acquired:
                if not acquired:
                    print("[skip] another sweep holds the lock; its run covers these sources")
                    return 0
                result = _sweep(
                    repo,
                    since,
                    why,
                    cutoff,
                    roots,
                    # Generations measured HERE rather than inside `_sweep`,
                    # for the same reason `identities` is: the reading has to
                    # be taken before the harvest can move anything, and
                    # passing it in is what lets a test hand `_sweep` a stale
                    # one.
                    # None: the BASELINE banks every file. The reading it is
                    # compared against measures only the historical ones, and
                    # `generation_conflict` tests containment between them.
                    Probe(present, named, identities, _store_generations(roots, present, None)),
                )
                return result if result else _finish_with_pin_export(repo, args.feedback_dir)
        except LockUnavailable as exc:
            return _refuse(
                repo,
                f"{exc}. Refusing to harvest: this is not lock contention, so "
                "no other run covers these sources, and skipping would leave "
                "the sweep silently disabled on this checkout.",
                record=True,
            )

    try:
        prompts = harvest_all(repo, since, roots)
    except (sqlite3.Error, OSError) as exc:
        # The read-only twin of the guard in `_sweep`. `--stats` exists to
        # REPORT that the sweep is stuck, so dying on the same corrupt page the
        # sweep dies on makes the diagnostic useless exactly when it is needed.
        # `record=False` because this command's contract is "report only, write
        # nothing", and its refusal is not a fact about the sweep's freshness.
        return _refuse(repo, f"reading a configured source failed: {exc!r}", record=False)
    # Same guard, same reason, as the one in `_sweep`: `pr_map` shells out to
    # `gh`, so an EMPTY harvest on a machine without it raised
    # FileNotFoundError and killed the READ-ONLY stats command outright - the
    # one command whose job is to report that the harvest was empty and what
    # the last refused write was. Found by Codex on #708 after the write path
    # was fixed and this one was not; they are guarded together now because
    # they harvest through one call.
    if prompts:
        try:
            attach_links(prompts, branch_windows(repo), pr_map(repo))
        except GhUnavailable as exc:
            # REPORTED, not recorded: this command writes nothing, and the
            # absence of `gh` is not a fact about the sweep's freshness. It
            # still has to say so rather than crash - diagnosing exactly this
            # is what --stats is for.
            return _refuse(repo, str(exc), record=False)

    result = _report_stats(repo, prompts, since, why)
    if args.feedback_dir is None:
        print("[UNAVAILABLE] pin export: no --feedback-dir was configured", file=sys.stderr)
    return result


def _sweep(
    repo: Path,
    since: float,
    why: str,
    cutoff: float,
    roots: SourceRoots | None = None,
    probe: Probe | None = None,
) -> int:
    """Harvest, link and write, under the caller's lock."""
    probe = Probe() if probe is None else probe
    sources_id = (SourceRoots() if roots is None else roots).identity()
    # Captured BEFORE the first source is read, against THIS run's cutoff, so
    # the snapshot at the end can be compared against the same population.
    # `identities` next door is the pre-lock device:inode probe, which is blind
    # to anything happening INSIDE a store that keeps its inode. Codex found
    # the gap on #708: an old transcript restored after its harvester has
    # passed but before the final snapshot leaves the root unchanged, so this
    # run banks the newly-raised evidence as its own baseline and every later
    # run reads that file as already harvested. Permanently.
    generations_before = (
        _store_generations(
            SourceRoots() if roots is None else roots,
            probe.present,
            None,
        )
        if probe.generations is None
        else probe.generations
    )
    try:
        prompts = harvest_all(repo, since, roots)
    except (sqlite3.Error, OSError) as exc:
        # A SOURCE READ that failed, which the probes cannot promise to catch.
        # `cursor_store_defect` runs one `SELECT ... LIMIT 1`; corruption in a
        # later B-tree page passes that and raises here, part way through the
        # production harvest. Without this the traceback goes to the Stop
        # hook's /dev/null, the watermark is never written, and the sweep looks
        # like a clean turn every turn. Recorded and refused instead, with the
        # watermark deliberately left where it was so the next run retries.
        # Narrow on purpose: these two families are what reading a store can
        # raise, and anything else is a defect in this program that must not be
        # dressed up as an unavailable source. Codex found it on #708.
        return _refuse(
            repo,
            f"reading a configured source failed part way through: {exc!r}. "
            "Refusing to bank a watermark: the harvest is incomplete, and "
            "advancing it would hide every source this run did not reach.",
            record=True,
        )
    # Guarded, not merely cheap-when-empty: `pr_map` shells out to `gh`, so on
    # a machine without it an EMPTY harvest raises FileNotFoundError and the
    # sweep dies before advancing its watermark. A prompt-free turn would then
    # never record that it was clean. Restored after the refactor dropped it
    # (Codex, #708).
    #
    # And the NON-empty half of the same defect, which the guard above hid:
    # with prompts to link, a missing `gh` raised through this line into the
    # Stop hook's discarded streams, so the sweep died silently every turn on
    # any checkout without it. Refused rather than degraded to an empty link
    # map: rows written with no PR link look exactly like rows whose prompts
    # belong to no PR, and this run would bank a watermark over them. Codex
    # found it on #708, one round after the empty case.
    if prompts:
        try:
            attach_links(prompts, branch_windows(repo), pr_map(repo))
        except GhUnavailable as exc:
            return _refuse(
                repo,
                f"{exc}. Refusing to bank a watermark: the rows this run "
                "would write carry no PR link, and a later run would read "
                "them as prompts that belong to none.",
                record=True,
            )
    try:
        fresh, total = write_out(repo, prompts, committed_rows(repo))
    except RegressionError as exc:
        record_error(repo, str(exc))
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    # RE-MEASURED under the lock, not reused. The snapshot above was taken
    # before `sweep_lock`, and a store unmounted between the two harvests zero
    # while every probe that would have noticed has already run. The sweep then
    # banks a watermark that RECORDS the vanished store as present, so the next
    # run sees no appearance, stays incremental, and skips everything added to
    # that store before the cutoff - permanently, because nothing ever reports
    # a difference again. Codex found it on #708, after the between-RUNS case
    # was fixed and this within-a-run one was not.
    #
    # Only the recorded set matters here, so this compares against the earlier
    # snapshot rather than against the last sweep: a store present when the run
    # started and absent when it ends did not contribute what this watermark is
    # about to claim it did.
    banked = probe.present
    gone, still_here, now_identities = _unreadable_stores(
        SourceRoots() if roots is None else roots, probe.named, banked
    )
    if gone:
        return _refuse(
            repo,
            "source(s) became unreadable during the harvest: "
            + "; ".join(gone)
            + ". Refusing to bank a watermark: this run's harvest cannot "
            "speak for a store that left while it ran, and recording it as "
            "present would keep every later run incremental over it.",
            record=True,
        )
    # The MIRROR case, and it loses the same files by the opposite route. A
    # store that was absent when the snapshot was taken and is here now was
    # harvested late or not at all, yet `still_here` would record it as
    # present - so the next run computes no appearance, honours the cutoff
    # this run is about to bank, and skips every file in that store older than
    # it. Which is all of them, for a store restored from a backup or synced
    # down from another machine. Refusing leaves `sources_present` as it was,
    # which is exactly what makes the NEXT run see the appearance and force
    # the full scan. Codex found it on #708, one round after the disappearance
    # half went in - one probe, two directions, and only one of them guarded.
    appeared = still_here - banked
    if appeared:
        return _refuse(
            repo,
            "source(s) became readable during the harvest: "
            + "; ".join(sorted(appeared))
            + ". Refusing to bank a watermark: this run harvested that store "
            "late or not at all, and recording it as present would let the "
            "next run stay incremental over files it has never read.",
            record=True,
        )

    # THE THIRD DIRECTION, and the one a NAME cannot see. A store atomically
    # replaced at the same configured path - a backup restore that preserves
    # transcript mtimes is the concrete case - reads as the same store to both
    # probes, so `gone` is empty and `appeared` is empty and the run banks a
    # watermark for a generation it never opened. Its files predate `since`, so
    # every later incremental sweep skips them, permanently, because nothing
    # ever reports a difference again. Device and inode is the identity that
    # notices; the name never could. Codex found it on #708, one round after
    # the appearance half, and it is the same defect both of those were.
    #
    # Compared only where BOTH probes saw the store: a store missing from
    # either side is the disappearance or appearance case, already refused
    # above, and re-reporting it here would name one defect twice.
    before = probe.identities
    replaced = sorted(
        f"{flag} ({before[flag]} -> {now_identities[flag]})"
        for flag in before.keys() & now_identities.keys()
        if before[flag] != now_identities[flag]
    )
    if replaced:
        return _refuse(
            repo,
            "source(s) were replaced at the same path during the harvest: "
            + "; ".join(replaced)
            + ". Refusing to bank a watermark: this run read the store that "
            "was there before, and recording it as present would let the next "
            "run stay incremental over a generation nothing has read.",
            record=True,
        )
    # THE FOURTH DIRECTION: the store is the same store, and its CONTENTS
    # changed under the harvest. A transcript restored after its harvester
    # walked past it and before this snapshot is not in the ledger this run
    # just wrote, yet the generation measured now records it - so the next run
    # compares equal, honours the cutoff, and skips it for good. Refusing is
    # the same answer the replacement case gets, for the same reason: this run
    # cannot speak for a generation it did not read. Codex found it on #708,
    # one round after the between-runs and arriving-history halves went in.
    #
    # Measured against the SAME cutoff on both sides, or the two would count
    # different populations and any difference would be meaningless. Compared
    # only where both snapshots saw the store, for the reason above.
    roots_now = SourceRoots() if roots is None else roots
    generations_now = _store_generations(roots_now, still_here, None)
    observed_now = _store_generations(roots_now, still_here, cutoff)
    changed = sorted(
        name
        for name in generations_before.keys() & observed_now.keys()
        if generation_conflict(generations_before[name], observed_now[name])
    )
    if changed:
        return _refuse(
            repo,
            "source(s) changed underneath the harvest: "
            + ", ".join(changed)
            + ". Refusing to bank a watermark: history arrived or was replaced "
            "inside a store while this run was reading it, and recording the "
            "generation measured now would mark files this harvest never saw "
            "as already read.",
            record=True,
        )
    # Banked against THE CUTOFF THIS RUN IS BANKING, not carried over from the
    # pre-lock probe: the historical digest is a claim relative to that number,
    # and recording one measured against the previous sweep's cutoff would
    # compare two different populations next time.
    write_watermark(repo, cutoff, sources_id, still_here, generations_now)
    print(f"[OK] {why}: +{fresh} new, {total} total -> {repo / INDEX_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
