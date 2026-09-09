#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Pack a rebuilt fixture set into a portable beatgrid bench bundle.

WHY A BUNDLE EXISTS AT ALL. The excerpt WAVs and the rekordbox ground truth can
only be produced on THIS Mac: the audio lives here, and the grids come from the
local rekordbox snapshot through the lane daemon. Every other host that has to
run this benchmark -- nucbox, agentbox, CI -- has neither. Without a bundle,
"same fixtures" across hosts is an assertion nobody can check, and a
cross-machine round-over-round delta means nothing.

WHY IT CARRIES CHECKSUMS AND NOT JUST FILES. A fixture set that is silently
different is worse than one that is obviously missing, because it still
produces a table. SHA256SUMS lets a consuming host verify byte-for-byte that
its excerpts are the ones the numbers were measured on, and `--verify` is the
check that says so. The manifest also carries the checksums inline, so a
consumer that only has the JSON can still tell whether its audio matches.

WHAT IS DELIBERATELY NOT HERE. No original audio: these are 45 second excerpts,
already the input every candidate reads, and shipping whole tracks would be
both enormous and a rights question nobody asked. No candidate output: a bundle
carries the QUESTION, and each host answers it independently.

NEVER COMMITTED. `data/` is gitignored. Pushing the bundle to the asset store
is the bench lane's job, not this script's; this only builds and verifies it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time

BUNDLE_SCHEMA = 1


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _pack_fixture(fixture: dict, wav_dir: str) -> dict:
    """Copy one excerpt into the bundle and describe it, or fail loudly.

    The checksum comparison against the manifest's own `wav_sha256` is the
    control: a manifest and a set of WAVs that have drifted apart would produce
    a bundle that looks complete and scores against different audio.
    """
    source = fixture["wav"]
    if not os.path.exists(source):
        raise SystemExit(
            f"[bundle] {source} is missing. Rebuild the excerpts first:\n"
            f"  uv run --no-project --script scripts/beatbench/fixtures.py "
            f"--rebuild-from <manifest> --out <manifest> --wav-dir <dir>"
        )
    name = f"{fixture['stable_id']}.wav"
    target = os.path.join(wav_dir, name)
    shutil.copyfile(source, target)
    digest = _sha256_file(target)

    if "wav_sha256" in fixture and fixture["wav_sha256"] != digest:
        raise SystemExit(
            f"[bundle] {name} hashes {digest} but the manifest recorded "
            f"{fixture['wav_sha256']}; the excerpt changed under the manifest"
        )

    return {
        "stable_id": fixture["stable_id"],
        "wav": f"wav/{name}",
        "sha256": digest,
        "bytes": os.path.getsize(target),
        "is_dynamic": fixture["is_dynamic"],
        "rb_bpm": fixture["rb_bpm"],
        "duration_ms": fixture["duration_ms"],
        "window_start_s": fixture["window_start_s"],
        "window_end_s": fixture["window_end_s"],
        "score_start_s": fixture["score_start_s"],
        "score_end_s": fixture["score_end_s"],
        "grid_beat_count": fixture["grid_beat_count"],
        "grid_bpm_span": fixture["grid_bpm_span"],
    }


def build(manifest_path: str, out_dir: str) -> int:
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    fixtures = manifest["fixtures"]
    if not fixtures:
        raise SystemExit(f"[bundle] {manifest_path} holds no fixtures; refusing to build")

    wav_dir = os.path.join(out_dir, "wav")
    os.makedirs(wav_dir, exist_ok=True)
    started = time.time()

    entries: list[dict] = []
    truth: dict[str, list] = {}
    total_bytes = 0
    for i, fixture in enumerate(fixtures, 1):
        entry = _pack_fixture(fixture, wav_dir)
        entries.append(entry)
        total_bytes += entry["bytes"]
        truth[fixture["stable_id"]] = fixture["ref_beats"]
        if i % 50 == 0:
            print(f"[bundle]   {i}/{len(fixtures)}", flush=True)

    truth_path = os.path.join(out_dir, "rekordbox-truth.json")
    with open(truth_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "schema": BUNDLE_SCHEMA,
                "note": (
                    "rekordbox ANLZ PQTZ beats inside each excerpt window, as "
                    "[n, t_seconds, bpm] triples in ABSOLUTE track time. n == 1 is "
                    "the downbeat. This is the ground truth every candidate is "
                    "scored against; it is rekordbox's opinion, not an independent "
                    "judgement of correctness."
                ),
                "beats": truth,
            },
            fh,
            indent=1,
        )

    bundle_manifest = {
        "schema": BUNDLE_SCHEMA,
        "name": "beatgrid-bench-v1",
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "built_from": manifest_path,
        # Everything report.py reads off a manifest travels with the bundle, so
        # a consuming host points the scorer straight at this file. The beats
        # themselves stay in the truth file (see `reads.truth`) rather than
        # being copied in beside these entries: one grid in two places is two
        # grids as soon as one of them is edited.
        # Each fixture's `wav` is written relative to THIS FILE, because a
        # bundle that named the machine it was built on would not be portable.
        # A bench manifest means something else by a relative path (relative to
        # the repo root), so the difference is declared rather than guessed at
        # by the reader. See `_harness.resolve_fixture_paths`.
        "paths_relative_to": "manifest",
        "excerpt": manifest["excerpt"],
        "denominator": manifest["denominator"],
        "population": manifest["population"],
        "selection": manifest["selection"],
        "seed": manifest.get("seed"),
        "counts": {
            "fixtures": len(entries),
            "fixed": sum(1 for e in entries if not e["is_dynamic"]),
            "dynamic": sum(1 for e in entries if e["is_dynamic"]),
            "wav_bytes": total_bytes,
        },
        "reads": {
            "truth": "rekordbox-truth.json",
            "checksums": "SHA256SUMS",
            "scorer": "apps/analysis_bench/scorers/beatgrid.py (version stamped in every artifact)",
        },
        "fixtures": entries,
    }
    manifest_out = os.path.join(out_dir, "manifest.json")
    with open(manifest_out, "w", encoding="utf-8") as fh:
        json.dump(bundle_manifest, fh, indent=1)

    sums_path = os.path.join(out_dir, "SHA256SUMS")
    with open(sums_path, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(f"{entry['sha256']}  {entry['wav']}\n")
        for extra in ("rekordbox-truth.json", "manifest.json"):
            fh.write(f"{_sha256_file(os.path.join(out_dir, extra))}  {extra}\n")

    bundle_digest = _sha256_file(sums_path)
    print(
        f"[bundle] {len(entries)} fixtures "
        f"({bundle_manifest['counts']['fixed']} fixed, "
        f"{bundle_manifest['counts']['dynamic']} dynamic), "
        f"{total_bytes / (1024 ** 3):.2f} GiB of wav, "
        f"built in {time.time() - started:.0f}s -> {out_dir}",
        flush=True,
    )
    print(f"[bundle] SHA256SUMS sha256 = {bundle_digest}", flush=True)
    return 0


def _expected_contents(bundle_dir: str) -> set[str]:
    """Every path the bundle's own manifest says it contains.

    SHA256SUMS CANNOT VOUCH FOR ITS OWN COMPLETENESS. Hashing each listed file
    catches a corrupted one, but a SHA256SUMS truncated after any whole line
    leaves `checked` nonzero and every unlisted WAV unexamined, so a half-copied
    bundle verified clean and could stand as release evidence (Codex P1
    BLOCKING on PR #1514). The listing has to be checked against an independent
    statement of what should be there, and the manifest is that statement.

    This is a completeness guard against truncation and partial copies, not a
    tamper guard: an editor who shortened SHA256SUMS *and* the manifest to match
    would satisfy both. Detecting that needs a signature the bundle does not
    carry, and claiming otherwise here would be the same defect one level up.
    """
    manifest_path = os.path.join(bundle_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        raise SystemExit(
            f"[bundle] no manifest.json in {bundle_dir}; nothing states what is expected"
        )
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    return {f["wav"] for f in manifest["fixtures"]} | {"rekordbox-truth.json", "manifest.json"}


def _scan_sums(bundle_dir: str, sums_path: str) -> tuple[set[str], int, list[str], list[str]]:
    """`(paths listed, files hashed, absent, mismatched)` from one SHA256SUMS."""
    listed: set[str] = set()
    checked = 0
    missing: list[str] = []
    bad: list[str] = []
    with open(sums_path, encoding="utf-8") as fh:
        for line in fh:
            digest, _, relative = line.strip().partition("  ")
            if not relative:
                continue
            listed.add(relative)
            path = os.path.join(bundle_dir, relative)
            if not os.path.exists(path):
                missing.append(relative)
                continue
            if _sha256_file(path) != digest:
                bad.append(relative)
            checked += 1
    return listed, checked, missing, bad


def verify(bundle_dir: str) -> int:
    """Re-hash every file the bundle claims and report the first disagreement.

    A control that can fail: it is pointed at the bundle's OWN checksum file, so
    a truncated copy, a partial rsync or a re-decode on a different ffmpeg comes
    out as a named mismatch rather than as a mysteriously different table. What
    the listing COVERS is checked separately, against the manifest, because a
    short listing is otherwise indistinguishable from a complete one.
    """
    sums_path = os.path.join(bundle_dir, "SHA256SUMS")
    if not os.path.exists(sums_path):
        raise SystemExit(f"[bundle] no SHA256SUMS in {bundle_dir}")

    expected = _expected_contents(bundle_dir)
    listed, checked, missing, bad = _scan_sums(bundle_dir, sums_path)

    print(f"[bundle] verified {checked} files in {bundle_dir}", flush=True)
    if checked == 0:
        # An empty checksum file must never read as a clean bundle.
        print("[bundle] FAILED: SHA256SUMS listed nothing, so nothing was verified")
        return 2

    unlisted = sorted(expected - listed)
    stray = sorted(listed - expected)
    if unlisted or stray:
        for name in unlisted[:10]:
            print(f"[bundle]   NOT LISTED IN SHA256SUMS {name}")
        for name in stray[:10]:
            print(f"[bundle]   LISTED BUT NOT IN THE MANIFEST {name}")
        print(
            f"[bundle] FAILED: SHA256SUMS covers {len(listed)} of the "
            f"{len(expected)} paths manifest.json declares "
            f"({len(unlisted)} unlisted, {len(stray)} stray); the listing is "
            f"incomplete, so a clean hash result would mean nothing"
        )
        return 3

    if missing or bad:
        for name in missing[:10]:
            print(f"[bundle]   MISSING {name}")
        for name in bad[:10]:
            print(f"[bundle]   CHANGED {name}")
        print(f"[bundle] FAILED: {len(missing)} missing, {len(bad)} changed")
        return 1
    print(
        f"[bundle] OK: SHA256SUMS covers all {len(expected)} paths manifest.json "
        f"declares, and every one matches its recorded sha256"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", help="a rebuilt fixtures.json to pack")
    ap.add_argument("--out", help="bundle directory to write")
    ap.add_argument("--verify", help="verify an existing bundle directory instead")
    args = ap.parse_args()

    if args.verify:
        return verify(args.verify)
    if not (args.manifest and args.out):
        raise SystemExit("[bundle] pass --manifest and --out to build, or --verify to check")
    return build(args.manifest, args.out)


if __name__ == "__main__":
    sys.exit(main())
