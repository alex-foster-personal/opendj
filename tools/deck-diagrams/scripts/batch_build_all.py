#!/usr/bin/env python3
"""Fetch + bootstrap + HTML + checksum + hover AC for catalog devices.

Usage:
  uv run python tools/deck-diagrams/scripts/batch_build_all.py
  uv run python tools/deck-diagrams/scripts/batch_build_all.py --limit 20
  uv run python tools/deck-diagrams/scripts/batch_build_all.py --ids ddj-rev5,ddj-800
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DIAG = ROOT / "tools" / "deck-diagrams"
REF = ROOT / "docs" / "controller" / "reference"
CAT = DIAG / "catalog" / "controllers.json"
SCRIPTS = DIAG / "scripts"
AT = "https://downloads.support.alphatheta.com/software_info/dj-controllers"

# catalog id -> preferred PDF filename stem / model folder
ID_TO_MODEL = {
    "ddj-flx10": "DDJ-FLX10",
    "ddj-flx4": "DDJ-FLX4",
    "ddj-flx6": "DDJ-FLX6",
    "ddj-flx6-2": "DDJ-FLX6-2",
    "ddj-flx2": "DDJ-FLX2",
    "ddj-1000": "DDJ-1000",
    "ddj-800": "DDJ-800",
    "ddj-rev1": "DDJ-REV1",
    "ddj-rev5": "DDJ-REV5",
    "ddj-rev7": "DDJ-REV7",
    "ddj-400": "DDJ-400",
    "ddj-200": "DDJ-200",
    "ddj-sb3": "DDJ-SB3",
    "ddj-sb2": "DDJ-SB2",
    "ddj-sx3": "DDJ-SX3",
    "ddj-sx2": "DDJ-SX2",
    "ddj-rr": "DDJ-RR",
    "ddj-1000srt": "DDJ-1000SRT",
    "xdj-xz": "XDJ-XZ",
    "xdj-rx3": "XDJ-RX3",
    "xdj-rx2": "XDJ-RX2",
    "xdj-az": "XDJ-AZ",
    "cdj-3000": "CDJ-3000",
    "cdj-2000nxs2": "CDJ-2000NXS2",
    "cdj-2000nxs": "CDJ-2000NXS",
    "djm-a9": "DJM-A9",
    "djm-900nxs2": "DJM-900NXS2",
    "djm-s7": "DJM-S7",
    "djm-s9": "DJM-S9",
    "djm-s11": "DJM-S11",
    "djm-v10": "DJM-V10",
    "djm-450": "DJM-450",
    "djm-750mk2": "DJM-750MK2",
    "opus-quad": "OPUS-QUAD",
    "omnis-duo": "OMNIS-DUO",
    "ddj-grv6": "DDJ-GRV6",
    "ddj-flx8": "DDJ-FLX8",
}


def run(cmd: list[str], check: bool = True) -> int:
    print("+", " ".join(cmd))
    p = subprocess.run(cmd)
    if check and p.returncode != 0:
        return p.returncode
    return p.returncode


def curl_pdf(url: str, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["curl", "-fsSL", "--connect-timeout", "12", "--max-time", "60", url, "-o", str(dest)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if r.returncode != 0 or not dest.exists():
        dest.unlink(missing_ok=True)
        return False
    if dest.read_bytes()[:5] != b"%PDF-":
        dest.unlink(missing_ok=True)
        return False
    return True


def find_existing_pdf(model: str) -> Path | None:
    patterns = [
        f"{model}_MIDI_Message_List_E1.pdf",
        f"{model}_MIDI_message_List_E1.pdf",
        f"{model}_MIDI_Message_List.pdf",
        f"Reloop-Mixtour_MIDI-Map.pdf" if "MIXTOUR" in model.upper() else "",
    ]
    for name in patterns:
        if not name:
            continue
        p = REF / name
        if p.exists() and p.stat().st_size > 500:
            return p
    # fuzzy
    for p in REF.glob("*.pdf"):
        if model.lower().replace("-", "") in p.name.lower().replace("-", ""):
            return p
    return None


def fetch_model(model: str) -> Path | None:
    existing = find_existing_pdf(model)
    if existing:
        return existing
    names = [
        f"{model}_MIDI_message_List_E1.pdf",
        f"{model}_MIDI_Message_List_E1.pdf",
        f"{model}_MIDI_message_List.pdf",
    ]
    dest = REF / f"{model}_MIDI_Message_List_E1.pdf"
    for n in names:
        url = f"{AT}/{model}/{n}"
        if curl_pdf(url, dest):
            print(f"  fetched {url}")
            return dest
    return None


def device_ready(device_id: str) -> bool:
    d = DIAG / "devices" / device_id
    return (d / "deck.html").is_file() and (d / "layout.json").is_file()


def bootstrap(device_id: str, model: str, pdf: Path, vendor: str) -> bool:
    rc = run(
        [
            "uv",
            "run",
            "python",
            str(SCRIPTS / "bootstrap_from_midi_pdf.py"),
            "--id",
            device_id,
            "--model",
            model,
            "--pdf",
            str(pdf),
            "--vendor",
            vendor,
        ],
        check=False,
    )
    return rc == 0


def post_checks(device_id: str) -> dict:
    d = DIAG / "devices" / device_id
    out: dict = {"id": device_id}
    r1 = subprocess.run(
        ["uv", "run", "python", str(SCRIPTS / "checksum.py"), str(d)],
        capture_output=True,
        text=True,
    )
    out["checksum"] = "ok" if r1.returncode == 0 else "fail"
    r2 = subprocess.run(
        ["uv", "run", "python", str(SCRIPTS / "verify_hover_ac.py"), str(d)],
        capture_output=True,
        text=True,
    )
    out["hover"] = "ok" if r2.returncode == 0 else "fail"
    out["checksum_log"] = (r1.stdout or "")[-200:]
    out["hover_log"] = (r2.stdout or "")[-200:]
    # control count
    try:
        n = len(json.loads((d / "layout.json").read_text()).get("controls") or [])
    except Exception:
        n = 0
    out["controls"] = n
    return out


def update_catalog_status(results: list[dict]) -> None:
    cat = json.loads(CAT.read_text())
    by = {r["id"]: r for r in results}
    for c in cat["controllers"]:
        r = by.get(c["id"])
        if not r:
            continue
        st = c.setdefault("status", {})
        if r.get("checksum") == "ok":
            st["docs"] = "ok"
            st["midi"] = "ok"
            st["diagram"] = "ok"
            st["checksum"] = "ok"
        if r.get("hover") == "ok":
            st["hover"] = "ok"
        elif r.get("hover"):
            st["hover"] = r["hover"]
    cat["updated"] = "2026-07-24"
    CAT.write_text(json.dumps(cat, indent=2) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ids", default="")
    ap.add_argument("--tier", type=int, default=1, help="only this doc_tier (default 1)")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    cat = json.loads(CAT.read_text())
    items = sorted(cat["controllers"], key=lambda x: x["popularity_rank"])
    if args.ids:
        want = set(args.ids.split(","))
        items = [c for c in items if c["id"] in want]
    else:
        items = [c for c in items if c.get("doc_tier") == args.tier]
    if args.limit:
        items = items[: args.limit]

    results: list[dict] = []
    for c in items:
        cid = c["id"]
        model = ID_TO_MODEL.get(cid) or c["model"].replace(" ", "-")
        vendor = c.get("brand", "unknown")
        print(f"\n=== {cid} ({model}) ===")
        if device_ready(cid) and not args.force:
            print("  already has deck.html - verifying")
            results.append(post_checks(cid))
            continue
        pdf = fetch_model(model)
        if not pdf:
            print("  no PDF - skip")
            results.append({"id": cid, "checksum": "pending", "hover": "pending", "controls": 0})
            continue
        # copy into docs already; bootstrap
        ok = bootstrap(cid, c["model"], pdf, vendor)
        if not ok:
            results.append({"id": cid, "checksum": "fail", "hover": "fail", "controls": 0})
            continue
        results.append(post_checks(cid))

    update_catalog_status(results)
    hover_ok = sum(1 for r in results if r.get("hover") == "ok")
    print(f"\nWAVE SUMMARY hover_ok={hover_ok}/{len(results)}")
    for r in results:
        print(f"  {r['id']}: hover={r.get('hover')} checksum={r.get('checksum')} n={r.get('controls')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
