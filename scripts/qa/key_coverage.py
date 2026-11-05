#!/usr/bin/env python3
"""Live key-coverage check for the maintainer's Viva / OLTF playlists (Wed 4 Nov 2026).

Requirement (the maintainer): "a test that simply determines whether I'm seeing keys for all the tracks in my
viva / OLTF playlists; >90% now, moving to 99%+ later."

  ✔︎ ✅ KEYCOV-01 a track counts as covered when the live app shows a key in its Key cell: a real key, or
    a best-guess key (shown dimmed with "?"). Blank, "failed" or "missing" is not covered.
      [if] a track has key="" and no key_guess [then ⛔️] it counts as uncovered
      [if] a track has only key_guess="7A" [then ⛔️ unless] it counts as covered (guess)
  ✔︎ ✅ KEYCOV-02 coverage is reported per playlist and overall, deduped by stable_id, with every
    uncovered track listed, and the run exits 1 when overall coverage is under --min (default 0.90).
      [if] overall coverage is 0.85 and --min 0.90 [then ⛔️ unless] exit code is 1
      [if] the app is unreachable [then ⛔️ unless] the run fails loudly, never reports 100%

Reads only, through the running app's own HTTP surface (`opendj api GET ...`).
Usage: python3 scripts/qa/key_coverage.py [--min 0.90] [--match 'viva|oltf'] [--json]
"""

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field


#----- config
class CFG:
    #: Ratchet (never silently): 0.90 from Wed 4 Nov 2026; 0.99 once the fallback chain and re-keys are proven.
    MIN_COVERAGE = 0.90
    PLAYLIST_MATCH = r"viva|oltf"
    TIMEOUT_S = 60


#----- app reads
def _api_get(path: str) -> object:
    out = subprocess.run(["opendj", "api", "GET", path], capture_output=True, text=True,
                         timeout=CFG.TIMEOUT_S, check=True).stdout
    return json.loads(out)


def _items(payload: object, *keys: str) -> list[dict]:
    if isinstance(payload, list):
        return payload
    elif isinstance(payload, dict):
        for k in keys:
            if isinstance(payload.get(k), list):
                return payload[k]
    raise RuntimeError(f"unexpected payload shape: {str(payload)[:200]}")


#----- model
@dataclass
class PlaylistCoverage:
    name: str
    total: int = 0
    real: int = 0
    guess: int = 0
    uncovered: list[str] = field(default_factory=list)

    @property
    def covered(self) -> int:
        return self.real + self.guess


def classify_key(track: dict) -> str:
    if (track.get("key") or "").strip():
        return "real"
    elif (track.get("key_guess") or "").strip():
        return "guess"
    elif True:
        return "none"


def measure(match: str) -> tuple[list[PlaylistCoverage], PlaylistCoverage]:
    playlists = [p for p in _items(_api_get("/api/v1/playlists"), "playlists", "items")
                 if re.search(match, p.get("name", ""), re.I)]
    if not playlists:
        raise RuntimeError(f"no playlists match /{match}/i")
    per, seen = [], {}
    for p in playlists:
        cov = PlaylistCoverage(p["name"])
        for t in _items(_api_get(f"/api/v1/playlists/{p['playlist_id']}/tracks"), "tracks", "items"):
            kind = classify_key(t)
            cov.total += 1
            if kind == "real":
                cov.real += 1
            elif kind == "guess":
                cov.guess += 1
            elif kind == "none":
                cov.uncovered.append(f"{t.get('artist', '')} - {t.get('title', '')} [{t.get('key_status')}]")
            seen[t.get("stable_id") or t.get("file_path")] = (kind, t)
        per.append(cov)
    overall = PlaylistCoverage("ALL (deduped)")
    for kind, t in seen.values():
        overall.total += 1
        if kind == "real":
            overall.real += 1
        elif kind == "guess":
            overall.guess += 1
        elif kind == "none":
            overall.uncovered.append(f"{t.get('artist', '')} - {t.get('title', '')} [{t.get('key_status')}]")
    return per, overall


#----- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=float, default=CFG.MIN_COVERAGE)
    ap.add_argument("--match", default=CFG.PLAYLIST_MATCH)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    per, overall = measure(a.match)
    pct = overall.covered / overall.total if overall.total else 0.0
    if a.json:
        print(json.dumps({"coverage": round(pct, 4), "min": a.min, "total": overall.total,
                          "real": overall.real, "guess": overall.guess, "uncovered": overall.uncovered,
                          "playlists": [{"name": c.name, "total": c.total, "covered": c.covered} for c in per]}))
    elif True:
        for c in per:
            print(f"{c.covered:>4}/{c.total:<4} {100 * c.covered / max(c.total, 1):5.1f}%  {c.name}")
        print(f"\nOVERALL {overall.covered}/{overall.total} = {100 * pct:.1f}% "
              f"(real {overall.real}, guess {overall.guess}); threshold {100 * a.min:.0f}%")
        for u in overall.uncovered:
            print(f"  [MISSING KEY] {u}")
    ok = pct >= a.min
    print(("[OK]" if ok else "[FAIL]") + f" key coverage {100 * pct:.1f}% vs {100 * a.min:.0f}%", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
