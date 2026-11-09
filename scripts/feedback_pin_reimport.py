"""Re-import comment pins lost to bulk harvest (issue #3782 step 1).

Usage::

    uv run python -m scripts.feedback_pin_reimport \\
        --audit (private path) \\
        --data-dir /path/to/data \\
        --archive /path/to/data/feedback/archive-20250912*.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Literal

from apps.shared.state import sync_stamp
from apps.webui.server.routes.feedback import _COMMENTS_FILE, _COMMENTS_LOCK, _load, _save

UpsertResult = Literal["added", "updated", "kept"]


def _edit_stamp(doc: dict[str, Any]) -> str | None:
    raw = doc.get("updated_at") or doc.get("created_at")
    if not isinstance(raw, str) or not sync_stamp.is_orderable(raw):
        return None
    return sync_stamp.to_canonical(raw)


def _upsert_pin(comments_path: Path, doc: dict[str, Any]) -> UpsertResult:
    with _COMMENTS_LOCK:
        items = _load(comments_path, "comments")
        by_id = {c.get("id"): i for i, c in enumerate(items)}
        pin_id = doc.get("id")
        if not isinstance(pin_id, str) or not pin_id:
            raise ValueError(f"pin doc missing id: {doc!r}")
        raw_status = doc.get("status")
        if raw_status in {None, "archived", "harvested"}:
            restored_status = "open"
        else:
            restored_status = raw_status
        restored = {**doc, "status": restored_status}
        restored.pop("harvested_at", None)
        if pin_id not in by_id:
            items.append(restored)
            _save(comments_path, "comments", items)
            return "added"

        live = items[by_id[pin_id]]
        live_stamp = _edit_stamp(live)
        arch_stamp = _edit_stamp(doc)
        should_replace = live_stamp is None or (arch_stamp is not None and arch_stamp > live_stamp)
        if not should_replace:
            return "kept"
        items[by_id[pin_id]] = restored
        _save(comments_path, "comments", items)
        return "updated"


def reimport_from_archive(comments_path: Path, archive_path: Path) -> tuple[int, int, int]:
    """Merge archive pins into the live store; live copies newer than the archive win."""
    payload = json.loads(archive_path.read_text(encoding="utf-8"))
    comments = payload.get("comments") if isinstance(payload, dict) else None
    if not isinstance(comments, list):
        raise ValueError(f"{archive_path} has no comments list")
    added = updated = kept = 0
    for doc in comments:
        if not isinstance(doc, dict):
            continue
        result = _upsert_pin(comments_path, doc)
        if result == "added":
            added += 1
        elif result == "updated":
            updated += 1
        else:
            kept += 1
    return added, updated, kept


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--archive", type=Path, action="append", default=[], required=True)
    args = parser.parse_args(argv)

    comments_path = args.data_dir / "feedback" / _COMMENTS_FILE
    comments_path.parent.mkdir(parents=True, exist_ok=True)
    if not comments_path.is_file():
        comments_path.write_text('{"comments": []}\n', encoding="utf-8")

    total_added = total_updated = total_kept = 0
    for archive in args.archive:
        added, updated, kept = reimport_from_archive(comments_path, archive)
        print(f"[OK] {archive.name}: {added} added, {updated} updated, {kept} kept (live newer)")
        total_added += added
        total_updated += updated
        total_kept += kept
    print(f"[OK] reimport complete: {total_added} added, {total_updated} updated, {total_kept} kept (live newer)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
