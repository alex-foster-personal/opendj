"""Turn a scanned stem inventory into a report, on stdout and as JSON.

Split out of ``stem_inventory.py`` so discovery and presentation can be read
and changed independently, and so both surfaces are built from ONE structure:
``print_report`` renders exactly what ``build_report`` returns, which is what
``--json`` writes. When the two were assembled separately the printed count
and the JSON count could disagree, and the printed one is the one people quote.

-Claude
"""
from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apps.stems import r2_stems

if TYPE_CHECKING:  # imported for types only, so there is no runtime cycle
    from apps.stems.inventory import Bundle, TrackResolver


def duplicate_ids(bundles: Iterable[Bundle]) -> dict[str, list[Bundle]]:
    """stable_ids held by more than one store.

    Reported, never resolved. Two renders of one track by different models are
    both legitimate, and which to keep is a listening decision, not a scripted
    one. Lives with the report rather than with discovery because being a
    duplicate is not a property of a bundle, only of a collection of them.
    """
    grouped: dict[str, list[Bundle]] = defaultdict(list)
    for bundle in bundles:
        if bundle.stable_id is not None:
            grouped[bundle.stable_id].append(bundle)
    return {sid: items for sid, items in sorted(grouped.items()) if len(items) > 1}


def r2_state_for(bundle: Bundle, remote: dict[str, int]) -> str:
    """Whether R2 already holds this bundle, judged by size and not by name.

    ``remote`` (from ``r2_stems.list_r2_sizes``) only ever lists the
    content-addressed ``assets/`` prefix, so the wanted keys must be derived
    the same way a publisher would derive them: from each file's own body
    SHA-256, not from ``bundle.key_for()``'s legacy
    ``stems/<preset>/<stable_id>/<filename>`` staging path. Matching on the
    legacy key against a content-addressed listing always misses, even for a
    fully published bundle.

    A key that exists at the wrong size is reported as "partial" rather than
    counted as present, because a truncated PUT still answers HTTP 200 and
    would otherwise be indistinguishable from a good upload.
    """
    if not bundle.is_publishable:
        return "not-publishable"
    wanted = {
        r2_stems.content_addressed_key(
            hashlib.sha256(path.read_bytes()).hexdigest()
        ): path.stat().st_size
        for path in bundle.files.values()
    }
    matched = sum(1 for key, size in wanted.items() if remote.get(key) == size)
    if matched == len(wanted):
        return "present"
    if matched == 0:
        return "absent"
    return "partial"


def _format_bytes(total: int) -> str:
    return f"{total / 1e9:.2f} GB" if total >= 1e9 else f"{total / 1e6:.0f} MB"

def build_report(
    bundles: list[Bundle],
    scanned_roots: list[Path],
    missing_roots: list[str],
    resolver: TrackResolver | None,
    remote: dict[str, int] | None,
) -> dict[str, Any]:
    """The whole inventory as data, so the CLI and the JSON agree by construction."""
    def _blank() -> dict[str, Any]:
        return {"bundles": 0, "bytes": 0, "forms": Counter(), "layouts": Counter(),
                "complete": 0, "publishable": 0, "identified": 0,
                "needs_manifest": 0}

    # Every scanned root is seeded, so a root that exists but holds nothing
    # still appears with a zero. A store that silently drops out of the report
    # the moment it empties is how a store gets forgotten.
    per_root: dict[str, dict[str, Any]] = {
        str(root): _blank() for root in scanned_roots
    }
    for bundle in bundles:
        entry = per_root.setdefault(str(bundle.root), _blank())
        entry["bundles"] += 1
        entry["bytes"] += bundle.total_bytes
        entry["forms"][bundle.form] += 1
        entry["layouts"][bundle.layout] += 1
        entry["complete"] += int(bundle.is_complete)
        entry["publishable"] += int(bundle.is_publishable)
        entry["needs_manifest"] += int(bundle.needs_manifest)
        entry["identified"] += int(bundle.stable_id is not None)
    for entry in per_root.values():
        entry["forms"] = dict(entry["forms"])
        entry["layouts"] = dict(entry["layouts"])

    report: dict[str, Any] = {
        "roots_scanned": sorted(per_root),
        "roots_missing": missing_roots,
        "per_root": per_root,
        "totals": {
            "bundles": len(bundles),
            "bytes": sum(b.total_bytes for b in bundles),
            "complete": sum(1 for b in bundles if b.is_complete),
            "publishable": sum(1 for b in bundles if b.is_publishable),
            "needs_manifest": sum(1 for b in bundles if b.needs_manifest),
            "identified": sum(1 for b in bundles if b.stable_id is not None),
            "ambiguous": sum(1 for b in bundles if len(b.candidate_ids) > 1),
            "loose": sum(1 for b in bundles if b.form == "loose"),
            "publishable_bytes": sum(
                b.total_bytes for b in bundles if b.is_publishable
            ),
        },
        "duplicate_stable_ids": {
            sid: [f"{b.root}/{b.slug}" for b in items]
            for sid, items in duplicate_ids(bundles).items()
        },
    }

    if resolver is not None:
        buckets = resolver.denominator()
        present = buckets.get("present", 0)
        awaiting = buckets.get("awaiting_volume", 0)
        covered = {
            b.stable_id for b in bundles
            if b.stable_id is not None
            and resolver.availability.get(b.stable_id) == "present"
        }
        report["coverage"] = {
            "availability_buckets": buckets,
            "denominator_present": present,
            "denominator_present_plus_awaiting": present + awaiting,
            "present_tracks_with_a_bundle": len(covered),
            "present_tracks_without_a_bundle": present - len(covered),
            "pct_of_present": round(100 * len(covered) / present, 1) if present else 0.0,
        }

    if remote is not None:
        states = Counter(r2_state_for(b, remote) for b in bundles)
        report["r2"] = {
            "objects_in_bucket": len(remote),
            "bundle_states": dict(states),
        }
    return report


def print_report(report: dict[str, Any], bundles: list[Bundle]) -> None:
    print("STEM BUNDLE INVENTORY")
    print("=" * 78)
    for root in report["roots_scanned"]:
        entry = report["per_root"][root]
        print(f"\n{root}")
        print(
            f"  {entry['bundles']:>4} bundles  {_format_bytes(entry['bytes']):>9}  "
            f"forms={entry['forms']}  layouts={entry['layouts']}"
        )
        print(
            f"       complete={entry['complete']}  identified={entry['identified']}  "
            f"publishable={entry['publishable']}  "
            f"needs_manifest={entry['needs_manifest']}"
        )
    for root in report["roots_missing"]:
        print(f"\n{root}\n  MISSING (root does not exist)")

    totals = report["totals"]
    print("\n" + "-" * 78)
    print(
        f"TOTAL {totals['bundles']} bundles, {_format_bytes(totals['bytes'])}; "
        f"{totals['complete']} complete, {totals['identified']} identified"
    )
    print(
        f"      {totals['publishable']} publishable to R2 "
        f"({_format_bytes(totals['publishable_bytes'])}), "
        f"{totals['needs_manifest']} blocked only on a missing manifest"
    )
    print(
        f"      {totals['loose']} are in LOOSE form, invisible to the stem loader"
    )
    if totals["ambiguous"]:
        print(
            f"      {totals['ambiguous']} have a source path that maps to "
            "SEVERAL stable_ids; owner undecidable here"
        )

    coverage = report.get("coverage")
    if coverage is not None:
        print("\nCOVERAGE (honest denominator, re-measured this run)")
        print(f"  availability buckets: {coverage['availability_buckets']}")
        print(
            f"  {coverage['present_tracks_with_a_bundle']} of "
            f"{coverage['denominator_present']} PRESENT tracks have a bundle "
            f"= {coverage['pct_of_present']}%"
        )
        print(
            f"  {coverage['present_tracks_without_a_bundle']} present tracks "
            "have no bundle"
        )
        print(
            "  denominator is 'present'; present+awaiting_volume would be "
            f"{coverage['denominator_present_plus_awaiting']}"
        )

    duplicates = report["duplicate_stable_ids"]
    if duplicates:
        print(f"\nDUPLICATE stable_ids across roots: {len(duplicates)}")
        print("  reported only. Deleting a render is a human decision.")
        for sid, paths in list(duplicates.items())[:10]:
            print(f"  {sid}")
            for path in paths:
                print(f"    {path}")
        if len(duplicates) > 10:
            print(f"  ... and {len(duplicates) - 10} more (see --json)")

    r2_block = report.get("r2")
    if r2_block is not None:
        print(f"\nR2 ({r2_block['objects_in_bucket']} stem objects in bucket)")
        print(f"  bundle states: {r2_block['bundle_states']}")

    flagged = [b for b in bundles if b.issues]
    if flagged:
        counted = Counter(issue for b in flagged for issue in b.issues)
        print("\nISSUES")
        for issue, count in counted.most_common():
            print(f"  {count:>4}  {issue}")

