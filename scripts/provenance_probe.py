"""What the sweep can see about its sources, at one instant.

Split out of `provenance_cli` when that module crossed the 600-line ceiling.
The seam is the subject: next door is a command - flags, exit codes, what to
print - while everything here answers one question about the world, taken twice
and compared. Which stores were readable, what each one WAS, and what each one
CONTAINED.

Every guard in `_sweep` is a difference between two of these readings, and the
reason there are three kinds of evidence rather than one is that each is blind
to a case the others catch: a NAME cannot see a store replaced at its own path,
a device and inode cannot see history arriving inside a directory that kept
them, and a count of historical files cannot see one of them swapped for
another. All three findings came from Codex on #708, in that order, as was
the fourth: comparing those file sets for EQUALITY made an open session's
next turn look like history arriving, so the comparison is containment.
"""

from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass, field
from pathlib import Path
from secrets import token_hex

from scripts.provenance_sources import (
    SourceRoots,
    cursor_store_defect,
    directory_store_defect,
)


@dataclass(frozen=True)
class Probe:
    """What the pre-harvest probe saw, in one object because it is one reading.

    Four values taken at the same instant and compared against a second reading
    of the same four at the end: which stores were readable, which paths the
    caller named, what each store WAS, and what each store CONTAINED. Passing
    them separately grew `_sweep` past the positional-argument ceiling, which
    was the linter noticing what the shape already said.

    `generations` is None only when the caller has not measured them, which is
    the tests' seam; the production path always passes them, because the whole
    point of that reading is that it happened BEFORE the harvest.
    """

    present: set[str] = field(default_factory=set)
    named: tuple[Path | None, Path | None, Path | None] = (None, None, None)
    identities: dict[str, str] = field(default_factory=dict)
    generations: dict[str, str] | None = None


IDENTITY_SEP = "|"
_TOKEN_SEP = ","


def _identity_tokens(root: Path, reference: float | None) -> str:
    """The regular files under `root`, as tokens that survive a file GROWING.

    `reference` None means EVERY regular file, which is what a run banks.
    A float means only those older than it, which is what a later run tests
    against that bank. The two are deliberately different populations: see
    `generation_conflict` for why the comparison is a subset test rather than
    an equality, and `_store_generations` for what it is defending.

    A token is a short hash of path, device, inode, size and mtime. An earlier
    revision dropped size and mtime, believing they had to go: appending a
    turn to an open session moves both, and under an EQUALITY comparison that
    was the false alarm being fixed. Under containment it is not, and a
    mutation putting them back left the whole suite green, which is a finding
    rather than a relief - the docstring was claiming a constraint the code no
    longer had. An appended file's mtime moves ABOVE the reference, so it
    leaves the tested population before its changed token can be asked about.

    They are back because they are strictly stronger. Content replaced IN
    PLACE - same path, same inode, mtime restored to the old value, which is
    what `cp -p` over a live transcript does - changes only the size, and the
    file stays historical, so it is tested and it fires. Identity alone is
    blind to exactly that case.

    Content is deliberately NOT hashed. Reading every transcript on every Stop
    hook is a different order of cost, and stat-level identity already answers
    the question this guard asks.

    WHAT THIS COSTS, measured rather than estimated, on the machine that runs
    it: `~/.claude/projects` holds 14,236 files and `~/.codex/sessions` 1,321,
    at 9 bytes of token each, so the watermark carries about 137 KB where the
    digest it replaces carried 32 bytes. It is one local JSON file written
    under an atomic rename once per sweep, and the walk that produces it was
    already being made for the digest, so the added cost is the write and the
    parse, not the traversal.

    A file that cannot be stat'ed is skipped rather than guessed at: it is one
    file's worth of imprecision in a value whose only job is to CHANGE when
    history arrives, and refusing the whole sweep over a racing temp file
    would trade a rare miss for a common outage.
    """
    tokens = set()
    for path in root.rglob("*"):
        try:
            info = path.stat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        if reference is not None and info.st_mtime >= reference:
            continue
        digest = hashlib.blake2b(digest_size=4)
        digest.update(
            f"{path.relative_to(root)}|{info.st_dev}|{info.st_ino}|"
            f"{info.st_size}|{info.st_mtime_ns}".encode()
        )
        tokens.add(digest.hexdigest())
    return _TOKEN_SEP.join(sorted(tokens))


def generation_conflict(banked: str, observed: str) -> bool:
    """Could the store `banked` describes have produced `observed`?

    NOT an equality, and the asymmetry is the fix. The population a run banks
    is every file in the store; the population a later run tests is only the
    files OLDER than the banked cutoff, because those are the ones the
    incremental harvest will skip. So the question is containment: is every
    historical file one the bank already knew about.

    Equality was the first version and Codex refuted it on #708. Under it, one
    more turn appended to an open session carried that transcript across the
    cutoff, out of the historical population and out of the digest, so the
    value changed because an agent spoke. Between runs that forced `since=0`
    and turned every Stop hook into the full multi-minute harvest the
    incremental sweep exists to avoid; mid-harvest it refused to bank a
    watermark at all. A first fix used the file's CREATION stamp to tell
    growth from arrival, which works and is not portable: CPython exposes
    `st_birthtime` on macOS and BSD and not on Linux, so it fixed the
    developer machines, left the defect on the runner and on agentbox, and CI
    failed the very tests written to prove it. Containment needs no stamp.

    Growth keeps a file's identity, so an appended transcript is still inside
    the bank. A restored or swapped file has an identity the bank never saw
    AND an old mtime, so it is tested and it fires. A file created after the
    bank has a new mtime and is never tested, which is right: the harvest
    reads it on its own.

    The store's own device and inode are compared for EQUALITY, in front of
    the containment test. That half answers a store REPLACED at its path,
    where containment would be the wrong question. An unmeasured store carries
    a nonce instead of an identity and so conflicts with everything including
    another unmeasured reading, which is what keeps an UNKNOWN from reading as
    evidence.
    """
    banked_identity, _, banked_files = banked.partition(IDENTITY_SEP)
    observed_identity, _, observed_files = observed.partition(IDENTITY_SEP)
    if banked_identity != observed_identity:
        return True
    return not set(_split(observed_files)) <= set(_split(banked_files))


def _split(joined: str) -> list[str]:
    """`str.split` on an empty string yields `['']`, which is one phantom file."""
    return joined.split(_TOKEN_SEP) if joined else []


def _store_generations(
    roots: SourceRoots, present: set[str], reference: float | None
) -> dict[str, str]:
    """Each present store's identity, as durably as this run can state it.

    Two parts, because a store can change in two ways that a name cannot see.
    Device and inode catch the whole store being REPLACED at its path. The
    historical digest catches history ARRIVING INSIDE it, or being swapped
    inside it. Both are needed: the first is blind to a restore into an
    existing directory, and the second only speaks for files older than the
    reference, which is what keeps ordinary work from moving it.

    The cursor store gets identity only. It is a single sqlite FILE that is
    written to continuously while an agent works, so any content-derived value
    moves on every sweep and would force a full harvest forever - the overshoot
    that turns a guard off. Replacing a single file necessarily changes its
    inode, so the identity half already covers it. `rglob` on a file yields
    nothing anyway; refusing to pretend otherwise is the point of saying so.

    `reference` decides WHICH population is measured, and the two callers want
    different ones. None banks every file, which is the claim a run records.
    A float tests only the files older than it, which are the ones a later
    incremental harvest would skip. `generation_conflict` is where the two
    meet, and it is a containment test rather than an equality precisely
    because they are not the same population.
    """
    directories = (("claude-root", roots.claude), ("codex-root", roots.codex))
    generations: dict[str, str] = {}
    for name, path in (*directories, ("cursor-db", roots.cursor_db)):
        if name not in present:
            continue
        try:
            info = path.stat()
        except OSError as exc:
            # UNMEASURED, and recorded as such rather than omitted. Dropping
            # the key was the silent version of the permanent loss these
            # generations exist to stop: every comparison over them intersects
            # the keys the two snapshots SHARE, so a store missing from one
            # side is not compared at all - the run still banks it in
            # `sources_present`, the next run finds nothing to disagree with,
            # honours the old cutoff, and skips every historical transcript
            # inside it for good. Codex found it on #708.
            #
            # The value carries a fresh nonce, so it is never equal to a real
            # identity and never equal to ANOTHER unmeasured reading, not even
            # one taken moments later in the same run. That is what makes it an
            # UNKNOWN rather than a verdict: it cannot be mistaken for evidence
            # in either direction, at any of the three sites that compare these
            # (mid-harvest, between runs, and the watermark read), and it does
            # not need each of those sites to remember to ask. Every one of
            # them already answers a difference with the safe response: refuse
            # to bank, or harvest in full.
            generations[name] = f"unmeasured:{name}:{exc.errno}:{token_hex(8)}"
            continue
        identity = f"{info.st_dev}:{info.st_ino}"
        is_directory = any(name == known for known, _ in directories)
        if is_directory:
            identity = f"{identity}{IDENTITY_SEP}{_identity_tokens(path, reference)}"
        generations[name] = identity
    return generations


def _unreadable_stores(
    roots: SourceRoots,
    named: tuple[Path | None, Path | None, Path | None],
    was_present: set[str] | None,
) -> tuple[list[str], set[str], dict[str, str]]:
    """Every configured source this run could not read, described.

    A source this run cannot read is UNAVAILABLE, not empty. The two look
    identical downstream and mean opposite things: an empty harvest is a
    measurement, an unreadable store is the absence of one. Let the second pass
    for the first and the run banks a watermark over sources it never opened,
    so every later incremental sweep skips them - for good, until someone
    thinks to pass --full. That is the same permanent silent loss the watermark
    fingerprint exists to stop, arriving through the CLI flags this revision
    added.

    TWO RULES, because "unreadable" and "absent" are not the same question:

    NAMED BUT ABSENT is always a defect. A default root may legitimately not
    exist - plenty of machines have never run Codex - and its harvester
    returning nothing is then TRUE. A path the caller typed and that is not
    there is a misspelling or an unmounted volume.

    PRESENT BUT UNREADABLE is always a defect, named or not. Existence says the
    tool is installed; it says nothing about whether this run read it. A
    `--codex-root` aimed at a regular file, or a zero-byte `--cursor-db`, both
    exist and both harvest nothing. This is the half the first version of this
    guard missed by asking only `.exists()`, found by Codex on #708 from
    evidence inside this repo: `test_provenance_cli.py` created its Cursor
    database with `.touch()` and the sweep exited 0 over it.

    The third return is a per-store IDENTITY, device and inode, for the stores
    that are present. A name is a weak identity: a store atomically replaced at
    the same configured path reads as the same store to both probes, so neither
    the disappearance rule nor the appearance rule fires and the run banks a
    watermark that vouches for a generation it never read. Codex found it on
    #708, one round after the appearance half.

    It is persisted too, and answers a DIFFERENT question on each side of the
    run. Between this run's two probes a difference means the harvest read a
    generation it cannot vouch for, so the sweep refuses. Between two runs it
    means the banked cutoff belongs to a store that is gone, so the next sweep
    re-scans in full. See `read_watermark`, which is where the second half
    lives; the first version recorded nothing and left that half open.
    """
    stores = (
        ("--claude-root", roots.claude, named[0], directory_store_defect),
        ("--codex-root", roots.codex, named[1], directory_store_defect),
        ("--cursor-db", roots.cursor_db, named[2], cursor_store_defect),
    )
    defects = []
    present = set()
    identities: dict[str, str] = {}
    # `explicit`, not `named` again: rebinding the parameter inside its own
    # loop reads as if the tuple were being consumed, and mypy flags the
    # assignment for the same reason a human misreads it.
    for flag, path, explicit, probe in stores:
        if not path.exists():
            if explicit is not None:
                defects.append(f"{flag} {path}: named but does not exist")
            elif was_present is not None and flag.lstrip("-") in was_present:
                # A DEFAULT store that a previous sweep for this same source
                # set found, and that is not here now. "Never installed" is a
                # measurement of nothing and stays silent; this is not that.
                # An unmounted home or a half-finished restore harvests zero
                # while EXISTING as far as the run can tell, and the successful
                # sweep then banks a watermark over sources it never read - so
                # when the store returns, everything older than that cutoff is
                # skipped for good. Codex found it on #708.
                defects.append(f"{flag} {path}: was present at the last sweep and is gone now")
            continue
        present.add(flag.lstrip("-"))
        # Between `exists()` and here a store can still go, and that is the
        # disappearance case rather than a crash: it is reported as a defect
        # on this same probe, which is where every other unreadable store is
        # reported too.
        try:
            info = path.stat()
        except OSError:
            defects.append(f"{flag} {path}: exists but cannot be stat'ed")
            continue
        identities[flag.lstrip("-")] = f"{info.st_dev}:{info.st_ino}"
        reason = probe(path)
        if reason is not None:
            defects.append(f"{flag} {path}: {reason}")
    return defects, present, identities
