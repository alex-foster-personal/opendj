"""Verify the F09 preload1-folded-bar-v1 real-input contract against pack metadata / local data.

Never downloads private audio zips. Missing coverage is UNAVAILABLE with owner the maintainer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = (
    ROOT / ".planning" / "releases" / "v2-2026-10-19-testing" / "real-input-contracts" / "preload1-folded-bar-v1.json"
)
DEFAULT_REPO = os.environ.get("ODJ_FIXTURE_REPO", "private_owner/odj-private")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def _fetch_pack_json(tag: str) -> tuple[dict, str]:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="preload1-pack-meta-") as tmp:
        dest = Path(tmp)
        subprocess.check_call(
            [
                "gh",
                "release",
                "download",
                tag,
                "--repo",
                DEFAULT_REPO,
                "--pattern",
                "pack.json",
                "--dir",
                str(dest),
            ]
        )
        body = (dest / "pack.json").read_bytes()
    return json.loads(body.decode("utf-8")), _sha256(body)


def _local_missing(data_dir: Path, required: list[str]) -> list[str] | None:
    db = data_dir / "state" / "state.db"
    if not db.is_file():
        return list(required)
    if (data_dir / "fixture-revision.txt").is_file():
        return list(required)
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        qmarks = ",".join("?" for _ in required)
        # Live rows only: removal tombstones a track by setting deleted_at, keeping the row.
        present = {
            str(sid)
            for (sid,) in con.execute(
                f"SELECT stable_id FROM tracks WHERE deleted_at IS NULL AND stable_id IN ({qmarks})",
                required,
            )
        }
        return [sid for sid in required if sid not in present]
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="fixture-pack-20261013-095156")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument(
        "--write-receipt",
        type=Path,
        default=CONTRACT.with_suffix(".receipt.json"),
    )
    args = parser.parse_args()
    contract = _load_contract()
    required = list(contract["required_stable_ids"])
    pack, pack_sha = _fetch_pack_json(args.tag)
    pack_ids = set(pack.get("stable_ids") or [])
    missing_pack = [sid for sid in required if sid not in pack_ids]
    missing_local = _local_missing(args.data_dir, required)
    receipt = {
        "contract_id": contract["contract_id"],
        "verified_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pack_tag": args.tag,
        "pack_json_sha256": pack_sha,
        "required_stable_ids": required,
        "missing_in_pack": missing_pack,
        "missing_in_data_dir": missing_local,
        "data_dir": str(args.data_dir),
        "status": "UNAVAILABLE" if missing_pack or missing_local != [] else "OK",
        "owner_if_unavailable": "the maintainer",
    }
    args.write_receipt.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, indent=2))
    return 1 if receipt["status"] != "OK" else 0


if __name__ == "__main__":
    sys.exit(main())
