"""Publish prompt ledgers into a destination store: a union that never drops a row.

The provenance sweep writes only its own ledger store, outside every tracked
working tree (the provenance-ledger-off-tree ADR). Getting those rows into the
shared record is this separate, explicit step, run by a person or a scheduled
job and never by a session hook:

  uv run --no-project python -m scripts.provenance_publish \\
    --dest <odj-private>/threads --from ~/.local/state/music-dj-tools/provenance/*/prompts.jsonl

then commit `prompts.jsonl`, `PROMPT-INDEX.md` and `user-prompts-pins.jsonl`
in the destination repo.

The same command seeds a store or folds in a stray ledger (a rescue branch, a
checkout's dirty copy): every `--from` is unioned into `--dest` through the
sweep's own `write_out`, so the row identity, the identity-field protection and
the superset guard are the sweep's, not a second implementation of them. A
`user-prompts-pins.jsonl` beside a `--from` ledger (REQHYG-05) is unioned into
the destination's pin ledger by pin id.

Requirements (mini-PRD)
  1. Union, never loss                                          [OK] done
     - if a row is in --dest before the run then it is in --dest after it
     - if a row is in any --from then it is in --dest after the run
     - if a row is already in --dest then every field it set is kept, so a
       lagging host cannot roll its branch or PR links back; a field it lacks
       is filled from any input that has it
     - if a pin id is in --dest or beside any --from then it is in the
       destination pin ledger after the run, at its newest updated_at
     - if one pin id carries two different rows updated_at cannot order then
       exit 2 and write nothing
     - if the output would drop any of these then exit 2 and write nothing
  2. Absent input is an error, not an empty input               [OK] done
     - if a --from path does not exist then exit 1 before writing
     - if a --from file holds no parseable row then exit 1 before writing
  3. Idempotent                                                 [OK] done
     - if the same --from is published twice then the second run writes
       byte-identical files and reports +0
  4. Serialized                                                 [OK] done
     - if another publish, sweep or pin export holds either destination lock
       then exit 3
       and write nothing, rather than both reading one prior ledger and the
       later replace dropping the earlier one's rows (Codex and Sol, #7403)
     - the lock spans reading the prior ledgers, writing, and re-verifying

Exit codes: 0 published, 1 a --from is absent or empty, 2 a write would drop
a row (or a ledger is unreadable), 3 the destination lock is held or locking
does not work there, 4 writing the destination failed (OSError). A 4 can leave
the prompt ledger written and the pin ledger not; every write is a union, so
re-running the same publish after fixing the destination completes it.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    from scripts.feedback_prompts_export import LEDGER as PINS
    from scripts.feedback_prompts_export import LOCKFILE as PINS_LOCKFILE
    from scripts.provenance_index import INDEX, _without_swept_stamp, render_index
    from scripts.provenance_state import LOCKFILE, LockUnavailable, _write_atomic, sweep_lock
    from scripts.provenance_sweep import (
        LEDGER,
        LedgerUnreadable,
        RegressionError,
        parse_ledger,
        read_ledger_file,
        row_key,
        stored_rows,
        write_out,
    )
except ModuleNotFoundError as exc:
    if exc.name == "scripts":
        raise SystemExit("uv run --no-project python -m scripts.provenance_publish") from None
    raise


@dataclass(frozen=True)
class Published:
    """What one publish wrote: new prompt rows, total prompt rows, total pins."""

    fresh: int
    total: int
    pins: int


class DestinationBusy(RuntimeError):
    """Another publish, sweep or pin export holds one of the destination's locks."""


class PinConflict(RegressionError):
    """One pin id carries two different rows that no revision stamp orders."""


# -----------------------------------------------------------------------------
# _helpers
# -----------------------------------------------------------------------------


def _fill(held: dict, arriving: dict) -> dict:
    """`held`, with any field it lacks (absent or None) taken from `arriving`.

    The merge rule for one prompt row seen in several ledgers. Prompt rows
    carry no revision stamp, so a field both copies set is never overwritten:
    a lagging host cannot roll a published link back. A field only one copy
    set is never lost either, so a richer copy listed later still contributes
    (Grok, #7403). Refusing on a field clash instead was measured against the
    real ledgers and rejected: across main, Silver's primary, the Air rescue
    branch and odj-private, the same row key differed in `source`, `ssid` or
    `branches` 324 times, because each host records its own session path.
    """
    filled = dict(held)
    for field, value in arriving.items():
        if filled.get(field) is None and value is not None:
            filled[field] = value
    return filled


def _read_sources(paths: list[Path]) -> dict[str, dict]:
    """Every row of every `--from`, refusing a source that is absent or empty."""
    rows: dict[str, dict] = {}
    for path in paths:
        if not path.is_file():
            raise ValueError(f"--from {path} does not exist; an absent ledger is not an empty one")
        found = read_ledger_file(path)
        if not found:
            raise ValueError(f"--from {path} holds no parseable ledger row")
        for key, rec in found.items():
            rows[key] = _fill(rows[key], rec) if key in rows else rec
    return rows


def _read_pins(path: Path) -> dict[str, dict]:
    """A pin ledger keyed by pin id (`ssid`); empty when the file is absent. Strict."""
    if not path.exists():
        return {}
    try:
        rows = parse_ledger(path.read_text(encoding="utf-8"), origin=str(path))
    except (UnicodeDecodeError, OSError) as exc:
        raise LedgerUnreadable(f"{path} cannot be read: {exc}") from exc
    pins: dict[str, dict] = {}
    for number, row in enumerate(rows, start=1):
        if not isinstance(row.get("ssid"), str) or not row["ssid"]:
            raise LedgerUnreadable(f"{path}: row {number} has no string ssid")
        # A pin id twice in ONE file goes through the same ordering as across
        # files, never last-line-wins (Sol, #7403).
        ssid = row["ssid"]
        pins[ssid] = _newer_pin(pins[ssid], row) if ssid in pins else row
    return pins


def _revision(pin: dict) -> datetime | None:
    """The pin's `updated_at` as an aware datetime, or None when it has none."""
    stamp = pin.get("updated_at")
    if not isinstance(stamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    # A naive stamp cannot be ordered against an aware one (comparing them
    # raises TypeError), so it counts as no stamp: the pair is then a
    # PinConflict refusal, not a traceback (Sol, #7403).
    return parsed if parsed.tzinfo is not None else None


def _newer_pin(held: dict, arriving: dict) -> dict:
    """The newer of two rows for one pin id, by `updated_at`; refuse if unordered.

    Argument order is not recency: a stale host published after a fresher one
    must not roll a pin's status back (Sol and Codex, #7403). Identical rows
    need no order; differing rows without two distinct stamps are refused.
    """
    if held == arriving:
        return held
    held_at, arriving_at = _revision(held), _revision(arriving)
    if held_at is None or arriving_at is None or held_at == arriving_at:
        raise PinConflict(
            f"pin {held['ssid']} has two different rows that updated_at cannot order "
            f"({held.get('updated_at')!r} vs {arriving.get('updated_at')!r}); refusing to pick one"
        )
    return arriving if arriving_at > held_at else held


def _merged_pins(dest: Path, sources: list[Path]) -> dict[str, dict]:
    """The destination's pins unioned with the pins beside every source, by pin id.

    Computed BEFORE anything is written, so an unreadable pin ledger or a
    `PinConflict` refuses the whole publish rather than half of it. No pin id
    is dropped, and one carried twice keeps its newest revision.
    """
    merged = _read_pins(dest / PINS)
    for source in sources:
        for ssid, pin in _read_pins(source.parent / PINS).items():
            merged[ssid] = _newer_pin(merged[ssid], pin) if ssid in merged else pin
    return merged


def _write_pins(dest: Path, merged: dict[str, dict]) -> int:
    """Write the merged pin ledger (deterministic order) and return its size."""
    if not merged:
        return 0
    rows = sorted(merged.values(), key=lambda row: (row.get("at") or "", row["ssid"]), reverse=True)
    body = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    target = dest / PINS
    if not target.exists() or target.read_text(encoding="utf-8") != body:
        _write_atomic(target, body)
    return len(rows)


def _index_is_stale(dest: Path) -> bool:
    """True when the written PROMPT-INDEX.md is not the render of the written ledger.

    Re-read after the write, under the lock: the pre-write superset check
    proves the INPUT to the render, not the file the render produced (Sol,
    #7403). Compared without the volatile "Swept" stamp. Not a subset test
    against the old index: the index is a newest-400 window, so new rows
    legitimately push old ones out of it while the ledger keeps them.
    """
    index = dest / INDEX
    try:
        written = index.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        raise LedgerUnreadable(f"{index} cannot be read: {exc}") from exc
    rendered = render_index(list(read_ledger_file(dest / LEDGER).values()))
    return _without_swept_stamp(written) != _without_swept_stamp(rendered)


def _snapshot(dest: Path) -> dict[str, bytes | None]:
    """The bytes of every file a publish may replace (None when absent)."""
    return {
        name: (dest / name).read_bytes() if (dest / name).is_file() else None
        for name in (LEDGER, INDEX, PINS)
    }


def _restore(dest: Path, before: dict[str, bytes | None]) -> None:
    """Put every file a publish may replace back to its snapshot."""
    for name, data in before.items():
        target = dest / name
        if data is None:
            target.unlink(missing_ok=True)
        elif not target.is_file() or target.read_bytes() != data:
            _write_atomic(target, data.decode("utf-8"))


def _missing_after_write(dest: Path, expected_rows: set[str], expected_pins: set[str]) -> int:
    """How many expected rows or pins the written files do NOT hold (presence, re-read)."""
    written_rows = {row_key(rec) for rec in read_ledger_file(dest / LEDGER).values()}
    return len(expected_rows - written_rows) + len(expected_pins - set(_read_pins(dest / PINS)))


# -----------------------------------------------------------------------------
# entry point
# -----------------------------------------------------------------------------


def _write_and_verify(
    dest: Path, arriving: list[dict], prior: dict[str, dict], expected: set[str], merged_pins: dict[str, dict]
) -> tuple[int, int, int]:
    """Write ledger, index and pins, then re-read them; raise RegressionError on any shortfall."""
    fresh, total = write_out(dest, arriving, prior)
    pins = _write_pins(dest, merged_pins)
    missing = _missing_after_write(dest, expected, set(merged_pins))
    if missing:
        raise RegressionError(f"{dest} is missing {missing} row(s) or pin(s) after the write")
    if _index_is_stale(dest):
        raise RegressionError(f"{dest / INDEX} is not the render of the written ledger")
    return fresh, total, pins


def publish(dest: Path, sources: list[Path]) -> Published:
    """Union every source ledger (and its sibling pin ledger) into `dest`.

    The destination lock is the sweep's own, so a publish also serializes with
    a sweep writing the same directory.
    """
    incoming = _read_sources(sources)
    dest.mkdir(parents=True, exist_ok=True)
    # BOTH locks, in the order a sweep takes them (its own, then the pin
    # export's), so a publish serializes with a sweep and with a standalone
    # pin export alike (Sol and Codex, #7403).
    with contextlib.ExitStack() as held:
        for lockfile in (LOCKFILE, PINS_LOCKFILE):
            if not held.enter_context(sweep_lock(dest, lockfile=lockfile)):
                raise DestinationBusy(f"{dest / lockfile} is held by another writer; nothing was written")
        prior = stored_rows(dest)
        merged_pins = _merged_pins(dest, sources)
        before = _snapshot(dest)
        # New rows as they are; a published row only gains fields it lacks.
        # write_out refreshes a known row's non-identity fields from its
        # input, which is right for a sweep re-harvesting its own sessions and
        # wrong here, where a lagging host would roll published branch and PR
        # links back (Codex, #7403). `_fill` keeps every field the published
        # row already set.
        arriving = [
            _fill(prior[key], rec) if key in prior else rec for key, rec in incoming.items()
        ]
        try:
            fresh, total, pins = _write_and_verify(dest, arriving, prior, set(prior) | set(incoming), merged_pins)
        except BaseException:
            # Put the destination back as it was, still under the locks, so
            # a failed postcondition never leaves the state it detected
            # (Sol, #7403).
            _restore(dest, before)
            raise
    return Published(fresh, total, pins)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dest", type=Path, required=True, help="destination ledger directory")
    ap.add_argument(
        "--from", dest="sources", type=Path, action="extend", nargs="+", required=True,
        help="one or more prompts.jsonl files to union into --dest (repeatable)",
    )
    args = ap.parse_args(argv)
    dest = args.dest.expanduser().resolve()
    try:
        done = publish(dest, [p.expanduser().resolve() for p in args.sources])
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    except RegressionError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    except (DestinationBusy, LockUnavailable) as exc:
        print(f"[UNAVAILABLE] {exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"[ERROR] writing {dest} failed: {exc}. Re-run the same publish once fixed.", file=sys.stderr)
        return 4
    print(f"[OK] +{done.fresh} new, {done.total} total -> {dest / LEDGER}; {done.pins} pins -> {dest / PINS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
