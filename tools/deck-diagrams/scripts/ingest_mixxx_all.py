#!/usr/bin/env python3
"""Ingest all Mixxx *.midi.xml into devices/ + extend catalog to reach ~100 hover-AC devices."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DIAG = ROOT / "tools" / "deck-diagrams"
CAT = DIAG / "catalog" / "controllers.json"
MIXXX = ROOT / ".tmp" / "mixxx-controllers" / "res" / "controllers"
INGEST = DIAG / "scripts" / "ingest_mixxx.py"


def slug_from_xml(name: str) -> str:
    base = name.replace(".midi.xml", "")
    s = base.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = re.sub(r"-+", "-", s)
    return s[:64]


def brand_guess(name: str) -> str:
    n = name.lower()
    for b in (
        "pioneer",
        "denon",
        "numark",
        "hercules",
        "reloop",
        "roland",
        "rane",
        "traktor",
        "native instruments",
        "behringer",
        "gemini",
        "akai",
        "allen and heath",
        "american audio",
        "dj techtools",
        "dj-tech",
        "vestax",
        "stanton",
        "mixvibes",
        "ion",
        "casio",
    ):
        if b in n or b.replace(" ", "-") in n.replace(" ", "-"):
            return b.title().replace("And", "and")
    return "Other"


def main() -> int:
    if not MIXXX.is_dir():
        print("missing mixxx checkout at", MIXXX, file=sys.stderr)
        return 1
    xmls = sorted(MIXXX.glob("*.midi.xml"))
    # Prefer DJ-ish names; still take enough to hit 100 devices total
    prefer = re.compile(
        r"(dj|ddj|cdj|djm|xdj|mix|kontrol|prime|sc\d|mc\d|inpulse|traktor|numark|hercules|reloop|roland|rane|denon|pioneer|behringer|gemini|xone|ns\d|mixtrack|fighter)",
        re.I,
    )
    ranked = sorted(xmls, key=lambda p: (0 if prefer.search(p.name) else 1, p.name))

    cat = json.loads(CAT.read_text())
    existing_ids = {c["id"] for c in cat["controllers"]}
    existing_devices = {p.name for p in (DIAG / "devices").iterdir() if p.is_dir()}

    # How many hover-ok already?
    need = 100
    # ingest until devices with layout controls >= need
    created = []
    for xml in ranked:
        cid = slug_from_xml(xml.name)
        if cid in existing_devices and (DIAG / "devices" / cid / "deck.html").exists():
            continue
        # skip tiny junk
        rc = subprocess.run(
            [
                "uv",
                "run",
                "python",
                str(INGEST),
                "--id",
                cid,
                "--xml",
                str(xml),
            ],
            capture_output=True,
            text=True,
        )
        if rc.returncode != 0:
            print("FAIL", cid, (rc.stderr or rc.stdout)[-180:])
            continue
        # ensure device exists with controls
        lay = DIAG / "devices" / cid / "layout.json"
        if not lay.exists():
            continue
        n = len(json.loads(lay.read_text()).get("controls") or [])
        if n < 4:
            print("SKIP tiny", cid, n)
            continue
        created.append((cid, n, xml.name))
        if cid not in existing_ids:
            rank = max(c["popularity_rank"] for c in cat["controllers"]) + 1
            cat["controllers"].append(
                {
                    "id": cid,
                    "brand": brand_guess(xml.name),
                    "model": xml.name.replace(".midi.xml", ""),
                    "popularity_rank": rank,
                    "popularity_notes": "Mixxx community map ingest",
                    "doc_tier": 2,
                    "status": {
                        "docs": "mixxx",
                        "midi": "ok",
                        "diagram": "ok",
                        "checksum": "n/a",
                        "hover": "pending",
                    },
                }
            )
            existing_ids.add(cid)
        existing_devices.add(cid)
        # stop when total devices with deck.html >= need + buffer
        decks = sum(
            1
            for p in (DIAG / "devices").iterdir()
            if p.is_dir() and (p / "deck.html").exists()
        )
        print(f"OK {cid} n={n} decks_total={decks}")
        if decks >= need + 5:
            break

    cat["controllers"] = sorted(cat["controllers"], key=lambda x: x["popularity_rank"])
    cat["updated"] = "2026-07-24"
    CAT.write_text(json.dumps(cat, indent=2) + "\n")
    print(f"created={len(created)} catalog={len(cat['controllers'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
