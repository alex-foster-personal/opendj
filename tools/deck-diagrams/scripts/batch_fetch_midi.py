#!/usr/bin/env python3
"""Fetch official MIDI PDFs for catalog tier-1 devices into docs/controller/reference/."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
REF = ROOT / "docs" / "controller" / "reference"
CAT = ROOT / "tools" / "deck-diagrams" / "catalog" / "controllers.json"

# (model_folder, filename_variants)
AT_BASE = "https://downloads.support.alphatheta.com/software_info/dj-controllers"


def candidates(model: str) -> list[str]:
    """URL candidates for AlphaTheta/Pioneer MIDI lists."""
    m = model.replace(" ", "-")
    names = [
        f"{m}_MIDI_message_List_E1.pdf",
        f"{m}_MIDI_Message_List_E1.pdf",
        f"{m}_MIDI_message_List.pdf",
        f"{m}_midi_message_list_e1.pdf",
    ]
    urls = [f"{AT_BASE}/{m}/{n}" for n in names]
    # pioneerdj media (often 403 but try)
    slug = m.lower()
    urls.append(
        f"https://www.pioneerdj.com/-/media/pioneerdj/software-info/controller/{slug}/{slug}_midi_message_list_e1.pdf"
    )
    return urls


KNOWN_EXTRA = {
    "reloop-mixtour": [
        "https://www.reloop.com/media/custom/upload/Reloop-Mixtour_MIDI-Map.pdf"
    ],
    "denon-lc6000": [
        # commonly mirrored; may 404 - leave for agent
    ],
}


def curl_to(url: str, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.check_call(
            [
                "curl",
                "-fsSL",
                "--connect-timeout",
                "15",
                "--max-time",
                "90",
                url,
                "-o",
                str(dest),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        dest.unlink(missing_ok=True)
        return False
    # basic PDF magic
    head = dest.read_bytes()[:5]
    if head != b"%PDF-":
        dest.unlink(missing_ok=True)
        return False
    return True


def main() -> int:
    cat = json.loads(CAT.read_text())
    ok = fail = skip = 0
    for c in cat["controllers"]:
        cid = c["id"]
        model = c["model"].replace(" ", "-")
        # normalize Pioneer model names
        model = model.replace("Traktor-Kontrol-", "Traktor-Kontrol-")
        out = REF / f"{model.replace('/', '-')}_MIDI_Message_List_E1.pdf"
        # special filenames already in repo
        alts = list(REF.glob(f"*{c['model'].split()[-1]}*MIDI*.pdf")) + list(
            REF.glob(f"*{model}*MIDI*.pdf")
        )
        if any(p.stat().st_size > 1000 for p in REF.glob("*.pdf") if model.lower() in p.name.lower() or c["model"].replace(" ", "-").lower() in p.name.lower()):
            print(f"SKIP exists-ish {cid}")
            skip += 1
            continue
        if c.get("doc_tier") != 1:
            print(f"SKIP tier2 {cid}")
            skip += 1
            continue
        urls = KNOWN_EXTRA.get(cid, []) + candidates(c["model"].replace(" ", "-"))
        # AlphaTheta folders often match model exactly
        got = False
        for url in urls:
            # choose dest name from model
            dest = REF / f"{c['model'].replace(' ', '-')}_MIDI_Message_List_E1.pdf"
            if dest.exists() and dest.stat().st_size > 1000:
                print(f"SKIP exists {cid}")
                skip += 1
                got = True
                break
            if curl_to(url, dest):
                print(f"OK {cid} <- {url}")
                ok += 1
                got = True
                break
        if not got:
            print(f"FAIL {cid}")
            fail += 1
    print(f"SUMMARY ok={ok} fail={fail} skip={skip}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
