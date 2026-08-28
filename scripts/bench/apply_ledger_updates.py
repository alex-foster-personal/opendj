# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Apply a saved rating round's ledger_updates to judgement-calibration.json.

Closes the loop of the self-judging experiment format. The rater page writes
``ledger_updates`` into the ratings JSON, each naming a judgement type and a
verdict that was fixed IN THE MANIFEST before the maintainer answered. This script moves
the counts and recomputes confidence. It cannot invent a verdict and it refuses
to apply the same round twice.

Requirements (mini-PRD):
  ✔︎ ✅ increments n_confirmed / n_contradicted per update and recomputes
    confidence as (n_confirmed + 1) / (n_confirmed + n_contradicted + 2).
    [if] an update names a judgement_id not in the ledger [then ⛔️] SystemExit
    [if] the same ratings file is applied twice [then ⛔️] SystemExit (idempotence
      by recorded source, no silent double-count)
    [if] a verdict is not confirms/contradicts [then ⛔️] SystemExit
  ✔︎ ✅ promotes status out of UNVERIFIED once n > 0, and appends the evidence row
    so a later reader can see which answer moved the number.
  ✔︎ ✅ --dry-run prints the diff and writes nothing.

Run:
  uv run scripts/bench/apply_ledger_updates.py --ratings scripts/bench/ratings/<file>.json
  uv run scripts/bench/apply_ledger_updates.py --ratings <file>.json --dry-run

-Claude
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

LEDGER = Path(__file__).parent / "judgement-calibration.json"
VERDICTS = {"confirms": "n_confirmed", "contradicts": "n_contradicted"}


def _confidence(confirmed: int, contradicted: int) -> float:
    """Laplace-smoothed posterior mean. n=0 returns exactly 0.5, i.e. we do not know."""
    return round((confirmed + 1) / (confirmed + contradicted + 2), 3)


def _status(judgement: dict, confirmed: int, contradicted: int) -> str:
    """UNVERIFIED only survives while n is genuinely 0."""
    if confirmed == 0 and contradicted == 0:
        return "UNVERIFIED"
    if contradicted == 0:
        return "VERIFIED"
    if confirmed == 0:
        return "FALSIFIED"
    return "PARTIAL"


def apply_updates(ledger: dict, updates: list[dict], source: str) -> list[str]:
    """Mutates ledger in place. Returns one human-readable line per change."""
    by_id = {j["id"]: j for j in ledger["judgements"]}
    applied = ledger.setdefault("applied_sources", [])
    if source in applied:
        raise SystemExit(
            f"{source} has already been applied to this ledger. Applying it again "
            "would double-count the same answers. Remove it from applied_sources "
            "only if you are deliberately re-running a corrected file."
        )

    lines: list[str] = []
    for upd in updates:
        jid = upd.get("judgement_id")
        verdict = upd.get("verdict")
        if jid not in by_id:
            raise SystemExit(
                f"ledger update names judgement_id {jid!r}, which is not in "
                f"{LEDGER.name}. Add the judgement type first, or fix the manifest."
            )
        if verdict not in VERDICTS:
            raise SystemExit(
                f"ledger update for {jid} has verdict {verdict!r}; expected one of "
                f"{sorted(VERDICTS)}. 'neither' answers must not reach this script."
            )
        j = by_id[jid]
        field = VERDICTS[verdict]
        before_conf = j["confidence"]
        before_status = j["status"]
        j[field] = j[field] + 1
        j["confidence"] = _confidence(j["n_confirmed"], j["n_contradicted"])
        j["status"] = _status(j, j["n_confirmed"], j["n_contradicted"])
        j.setdefault("evidence", []).append({
            "verdict": "confirmed" if verdict == "confirms" else "contradicted",
            "source": source,
            "question": upd.get("question", ""),
            "our_view": upd.get("our_view", ""),
            "his_answer": upd.get("his_answer", ""),
            "his_note": upd.get("his_note", ""),
        })
        lines.append(
            f"{jid}: {verdict:12s}  {before_status} -> {j['status']}   "
            f"confidence {before_conf:.3f} -> {j['confidence']:.3f}   "
            f"(n={j['n_confirmed']}C/{j['n_contradicted']}X)"
        )

    applied.append(source)
    _refresh_summary(ledger)
    return lines


def _refresh_summary(ledger: dict) -> None:
    """Keep the summary buckets truthful after a move."""
    buckets: dict[str, list[str]] = {
        "unverified_n_zero": [], "falsified_by_his_labels": [], "verified": [], "partial": []
    }
    key = {"UNVERIFIED": "unverified_n_zero", "FALSIFIED": "falsified_by_his_labels",
           "VERIFIED": "verified", "PARTIAL": "partial"}
    for j in ledger["judgements"]:
        buckets[key[j["status"]]].append(j["id"])
    summary = ledger.setdefault("summary", {})
    summary.update(buckets)
    summary["total_judgement_types"] = len(ledger["judgements"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ratings", required=True, type=Path)
    parser.add_argument("--ledger", type=Path, default=LEDGER)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.ratings.is_file():
        raise SystemExit(f"ratings file does not exist: {args.ratings}")
    if not args.ledger.is_file():
        raise SystemExit(f"ledger does not exist: {args.ledger}")

    payload = json.loads(args.ratings.read_text())
    updates = payload.get("ledger_updates")
    if updates is None:
        raise SystemExit(
            f"{args.ratings.name} carries no 'ledger_updates' key. It was saved by a rater "
            "page predating the prediction format, so there is nothing to apply."
        )
    if len(updates) == 0:
        print(f"[OK] {args.ratings.name} has 0 ledger updates, nothing to do")
        return

    ledger = json.loads(args.ledger.read_text())
    lines = apply_updates(ledger, updates, args.ratings.name)

    for line in lines:
        print("  " + line)
    if args.dry_run:
        print(f"[OK] dry run: {len(lines)} update(s) computed, ledger NOT written")
        return
    args.ledger.write_text(json.dumps(ledger, indent=2) + "\n")
    print(f"[OK] applied {len(lines)} update(s) to {args.ledger.name}")


if __name__ == "__main__":
    main()
