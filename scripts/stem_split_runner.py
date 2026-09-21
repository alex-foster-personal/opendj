"""Randomised-split stem farming: every track gets an arm, and the DB remembers.

the maintainer's idea, and it is free: these tracks are being separated anyway, so
assigning each one a different CONFIG turns a bulk job into an experiment. Six
months from now the question "does hdemucs_mmi beat htdemucs on drum-and-bass
but lose on soul" is answerable from data already on disk, instead of needing
a fresh benchmark nobody has time to run.

WHY ASSIGNMENT IS HASHED, NOT RANDOM. ``Math.random``-style assignment cannot
be reproduced, cannot be resumed after a crash without re-rolling, and quietly
re-randomises a track on every re-run. Hashing the stable_id gives the same
answer forever, balances arms across a large set, and lets a partial run resume
without changing anyone's assignment.

WHY THIS IS NOT A/B TESTING THE DEFAULT. Nobody is being served a worse product
to learn something: every arm here is a config this project already considers
shippable. The split buys COVERAGE of the option space across real material,
which is the thing every benchmark in this repo has lacked (n=1 track, one
genre, one window).

MINI-PRD
--------
Status key: `→` out of scope | `?` todo | `✔︎` done | `✔︎ ✅` done + ran + works
as expected | `✔︎ ✅ 🎯` done + working + regression tests.

  ✔︎ ✅ 🎯 assign each track an arm deterministically from its stable_id, so
    the same track always lands in the same arm.
    [if] the runner is invoked twice [then] every track keeps its arm
    [if] a track is added to the library later [then] it gets an arm without
         disturbing anyone else's
    [if] arms are added or removed [then ⛔️] assignments change -- the arm set
         is part of the experiment identity and is recorded per row

  ✔︎ ✅ 🎯 record the assignment in state.db BEFORE the run, not after.
    [if] the farm crashes mid-batch [then] the intent is still on record and
         the gap between intended and completed is visible
    [if] a row already exists for a track [then] it is not rewritten -- first
         assignment wins, so a re-run cannot silently relabel history

  ✔︎ ✅ arms are drawn from apps/stems/tiers.py plus explicit extra configs,
    never invented here.
    [if] an arm names a preset the farm cannot run [then ⛔️] refuse before
         spending anything

  → scoring the arms against each other. That needs true reference stems and
    lives in scripts/bench/. This only makes the data exist.
  → choosing a winner. A split with no listening test is a dataset, not a
    conclusion.

-Claude
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
EVENT_KIND: str = "stem_split_assignment"
# Bump when the arm set changes, so old rows stay interpretable rather than
# being silently mixed with rows drawn from a different option space.
# v2 Fri 24 Jul 2026: the delicious arm moved flac -> opus. The arm set is part
# of the experiment identity, so the id moves with it and v1 rows stay
# interpretable rather than being silently pooled with a different option space.
EXPERIMENT_ID: str = "stem-split-v2"


@dataclass(frozen=True)
class Arm:
    """One config in the split. ``preset`` must exist in the farm's PRESETS."""

    name: str
    preset: str
    codec: str
    weight: int = 1  # relative share of tracks


# The arms. Every one is a config this project already considers shippable --
# the split is for COVERAGE, not for serving anyone a worse product.
ARMS: tuple[Arm, ...] = (
    # The default carries most of the weight: this is a production run first
    # and an experiment second.
    Arm("optimal", "hdemucs_mmi-ov0.25", "opus", weight=6),
    # The rival that WINS BASS by 0.775 dB in the only four-stem run so far,
    # which is larger than the vocal advantage the default rests on.
    Arm("htdemucs", "htdemucs-ov0.25", "opus", weight=2),
    # The quality ceiling, for a per-genre read on how much is being left.
    # OPUS, NOT FLAC, for two reasons. It was the only arm whose codec differed,
    # so any listening preference for it confounded model with codec -- you
    # could not tell whether delicious sounded better because of htdemucs_ft or
    # because it was lossless. And it was the only arm with a memory problem:
    # flac bundles are ~2.7x the bytes, and publish holds queue-depth +
    # queue-workers of them in RAM at once.
    # Tier L in apps/stems/tiers.py KEEPS flac -- that is the scored control
    # arm, where lossless genuinely matters because SI-SDR against a lossy stem
    # measures the codec as well as the model. This is the coverage split, and
    # a different question.
    Arm("delicious", "htdemucs_ft-ov0.25", "opus", weight=1),
    # The cheap rung, to test whether overlap matters on real material rather
    # than on MUSDB.
    Arm("quick", "hdemucs_mmi-ov0.1", "opus", weight=1),
)


def _expanded_arms() -> list[Arm]:
    out: list[Arm] = []
    for arm in ARMS:
        out.extend([arm] * arm.weight)
    return out


def assign_arm(stable_id: str) -> Arm:
    """Deterministic arm for a track. Same id -> same arm, forever."""
    pool = _expanded_arms()
    digest = hashlib.sha256(f"{EXPERIMENT_ID}:{stable_id}".encode()).digest()
    return pool[int.from_bytes(digest[:8], "big") % len(pool)]


def _assert_arms_runnable() -> None:
    from scripts.modal_vocal_farm import PRESETS, STEM_CODECS

    for arm in ARMS:
        if arm.preset not in PRESETS:
            raise SystemExit(
                f"error: arm {arm.name!r} names preset {arm.preset!r}, which "
                f"the farm cannot run. Known: {', '.join(sorted(PRESETS))}"
            )
        if arm.codec not in STEM_CODECS:
            raise SystemExit(
                f"error: arm {arm.name!r} names codec {arm.codec!r}; "
                f"known: {', '.join(sorted(STEM_CODECS))}"
            )


def already_assigned(data_dir: Path) -> set[str]:
    """Tracks a previous wave has already claimed for this experiment."""
    db = data_dir / "state" / "state.db"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT stable_id FROM events WHERE kind = ?", (EVENT_KIND,)
            )
        }
    finally:
        conn.close()


def candidate_tracks(data_dir: Path, limit: int) -> list[str]:
    """Farmable tracks NOT already covered by a previous wave, longest first.

    EXCLUDING PRIOR WAVES IS THE WHOLE POINT AND WAS MISSING. This passes
    ``refarm=True`` so the vocal cache does not hide tracks, but that also
    meant every wave re-selected the same head of the list: a second
    ``--limit 100`` picked 100 tracks of which 98 already had an assignment,
    so the wave was 2 tracks of new coverage at the price of 100. Worse, each
    of those 98 already had a remote directory at its arm's preset, and
    ``scp -r`` into an existing directory NESTS rather than merges -- so the
    size verify would fail and delete the local bundle it had just made.
    """
    from scripts.modal_vocal_farm import compute_gap

    done = already_assigned(data_dir)
    fresh = [t.stable_id for t in compute_gap(data_dir, refarm=True)
             if t.stable_id not in done]
    return fresh[:limit] if limit else fresh


def record_assignments(
    data_dir: Path, pairs: list[tuple[str, Arm]], dry_run: bool
) -> int:
    """Write assignments to state.db events. First assignment wins."""
    db = data_dir / "state" / "state.db"
    conn = sqlite3.connect(db)
    written = 0
    try:
        existing = {
            row[0]
            for row in conn.execute(
                "SELECT stable_id FROM events WHERE kind = ?", (EVENT_KIND,)
            )
        }
        now = datetime.now(UTC).isoformat(timespec="seconds")
        for stable_id, arm in pairs:
            if stable_id in existing:
                continue  # first assignment wins; never relabel history
            if dry_run:
                written += 1
                continue
            conn.execute(
                "INSERT INTO events (ts, kind, stable_id, payload_json, actor) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    now,
                    EVENT_KIND,
                    stable_id,
                    json.dumps(
                        {
                            "experiment": EXPERIMENT_ID,
                            "arm": arm.name,
                            "preset": arm.preset,
                            "codec": arm.codec,
                            "arms": [a.name for a in ARMS],
                        }
                    ),
                    "stem_split_runner",
                ),
            )
            written += 1
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return written


def run_arm(arm: Arm, ids: list[str], dest: str, dry_run: bool) -> int:
    """Farm one arm's tracks in a single batch. Returns the exit code."""
    cmd = [
        sys.executable, "-m", "scripts.modal_vocal_farm",
        "--preset", arm.preset, "--stem-codec", arm.codec,
        "--stems-dest", dest, "--refarm",
    ]
    for stable_id in ids:
        cmd += ["--only-stable-id", stable_id]
    if dry_run:
        cmd.append("--dry-run")
    print(f"\n=== arm {arm.name} ({arm.preset}, {arm.codec}): {len(ids)} tracks ===",
          flush=True)
    return subprocess.run(cmd, cwd=str(REPO_ROOT), check=False).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.stem_split_runner")
    parser.add_argument("--limit", type=int, default=100,
                        help="tracks this wave (default 100; 0 = the whole gap)")
    parser.add_argument("--dest", default="bifrost2",
                        choices=("bifrost2", "local", "r2", "none"))
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--plan-only", action="store_true",
                        help="assign and record, run nothing")
    args = parser.parse_args(argv)

    _assert_arms_runnable()
    from apps.shared.paths import DATA_DIR

    data_dir = args.data_dir or DATA_DIR
    ids = candidate_tracks(data_dir, args.limit)
    if not ids:
        print("nothing to do: the gap is empty")
        return 0

    pairs = [(sid, assign_arm(sid)) for sid in ids]
    by_arm: dict[str, list[str]] = {}
    for sid, arm in pairs:
        by_arm.setdefault(arm.name, []).append(sid)

    print(f"wave of {len(ids)} tracks, experiment {EXPERIMENT_ID}")
    print(f"{'arm':<12}{'preset':<24}{'codec':<7}{'n':>5}{'share':>8}")
    print(f"{'-'*12}{'-'*24}{'-'*7}{'-'*5}{'-'*8}")
    for arm in ARMS:
        n = len(by_arm.get(arm.name, []))
        print(f"{arm.name:<12}{arm.preset:<24}{arm.codec:<7}{n:>5}"
              f"{n / len(ids) * 100:>7.1f}%")

    recorded = record_assignments(data_dir, pairs, args.dry_run or args.plan_only)
    print(f"\nassignments recorded: {recorded} new "
          f"({len(pairs) - recorded} already had one)")
    if args.plan_only:
        return 0

    failed = 0
    for arm in ARMS:
        arm_ids = by_arm.get(arm.name, [])
        if not arm_ids:
            continue
        code = run_arm(arm, arm_ids, args.dest, args.dry_run)
        if code != 0:
            print(f"arm {arm.name} exited {code}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
