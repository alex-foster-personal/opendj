"""Build a blind ear-rating manifest for novox_listen.html from ANY corpus.

The novox round-0 fixture was rated on MTG-Jamendo (external human labels).
Round 1 repeats it on MAINTAINER'S OWN MUSIC, where no labels exist and his ear is
the only truth - so the manifest carries `has_dataset_labels: false` and the
page hides the label column and its agreement tally rather than inventing one.

Rating time is the scarce resource, so tracks are stratified toward the
DECISION ZONE: everything at or below `--zone-max` coverage (where the
no-lyrics band actually bites) plus `--controls` high-coverage tracks as
sanity anchors, which should read obviously-vocal. Hash-ordered output so
coverage order leaks nothing.

Coverage files come from scripts/lyrics_stem_coverage.py --envelopes.

Usage (repo root):
    uv run --no-sync python scripts/bench/lyrics_earband_manifest.py \
        --source data/state/lyrics-eval/own-crate/vocal-presence.json:/owncrate \
        --source data/state/lyrics-eval/oltf/vocal-presence.json:/oltf \
        --out scripts/bench/lyrics_earband_manifest.json

-Claude
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[2]


def _audio_url(mount_dir: Path, mount: str, track_id: str) -> str:
    """The playable full-mix URL for a track, verified to exist on disk."""
    hits = sorted((mount_dir / "audio").glob(f"{track_id}.*"))
    if len(hits) != 1:
        raise SystemExit(f"[ERROR] {track_id}: expected 1 audio file in {mount_dir / 'audio'}, "
                         f"found {len(hits)} -- a rating page that 404s wastes the maintainer's time")
    return f"{mount}/audio/{hits[0].name}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", action="append", required=True, metavar="COVERAGE_JSON:MOUNT",
                    help="repeatable; e.g. data/.../oltf/vocal-presence.json:/oltf")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--zone-max", type=float, default=30.0,
                    help="rate every track at or below this coverage (default 30)")
    ap.add_argument("--controls", type=int, default=6,
                    help="high-coverage sanity anchors to include (default 6)")
    ap.add_argument("--corpus-name", default="the maintainer's own crate")
    args = ap.parse_args()

    zone: list[dict] = []
    high: list[dict] = []
    for spec in args.source:
        path_str, _, mount = spec.rpartition(":")
        if not path_str or not mount.startswith("/"):
            raise SystemExit(f"[ERROR] --source must be PATH:/mount, got {spec!r}")
        cov_path = Path(path_str)
        mount_dir = cov_path.parent
        rows = json.loads(cov_path.read_text(encoding="utf-8"))
        for r in rows:
            if "env_mix" not in r:
                raise SystemExit(f"[ERROR] {cov_path} has no envelopes -- re-run "
                                 f"scripts/lyrics_stem_coverage.py with --envelopes")
            entry = {
                "track_id": f"{mount.strip('/')}:{r['track_id']}",
                "coverage_pct": r["coverage_pct"],
                "vocal_s": r["vocal_s"],
                "duration_s": r["duration_s"],
                "audio_url": _audio_url(mount_dir, mount, r["track_id"]),
                "stem_url": f"{mount}/stems/{r['stem_vocals']}",
                "env_mix": r["env_mix"],
                "env_vox": r["env_vox"],
                "regions": r["regions"],
                "env_duration_s": r["duration_s"],
                "peak_vox_s": r["peak_vox_s"],
                "corpus": mount.strip("/"),
            }
            (zone if r["coverage_pct"] <= args.zone_max else high).append(entry)

    high.sort(key=lambda e: hashlib.sha1(e["track_id"].encode()).hexdigest())
    picked = zone + high[: args.controls]
    picked.sort(key=lambda e: hashlib.sha1(e["track_id"].encode()).hexdigest())
    payload = {
        "corpus_name": args.corpus_name,
        "has_dataset_labels": False,
        "zone_max": args.zone_max,
        "n_zone": len(zone),
        "n_controls": min(args.controls, len(high)),
        "display_cols": len(picked[0]["env_mix"]) if picked else 0,
        "tracks": picked,
    }
    args.out.write_text(json.dumps(payload), encoding="utf-8")
    print(f"[OK] {args.out} -- {len(picked)} tracks to rate "
          f"({len(zone)} in the <={args.zone_max}% decision zone, "
          f"{payload['n_controls']} high-coverage controls; "
          f"denominator: {len(zone) + len(high)} stemmed tracks across {len(args.source)} corpora)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
