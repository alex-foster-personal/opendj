"""Export active and archived user UI pins beside the prompt provenance ledger.

Usage:
  uv run --no-project python -m scripts.feedback_prompts_export \\
    --repo <checkout> --feedback-dir <data-dir>/feedback

The feedback directory is deliberately explicit. An absent or unreadable store
is not evidence of no user prompts, so this exporter refuses instead of writing
an empty ledger. Output defaults to the repository's main worktree, rather
than a linked worktree, matching the provenance sweep's session scope.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

try:
    from scripts.provenance_sources import main_worktree
except ModuleNotFoundError as exc:
    if exc.name == "scripts":
        raise SystemExit("uv run --no-sync python -m scripts.feedback_prompts_export") from None
    raise
from scripts.provenance_state import _write_atomic, sweep_lock

OUT_DIRNAME = "docs/threads"
LEDGER = "user-prompts-pins.jsonl"
COMMENTS = "comments.json"
LOCKFILE = ".feedback-prompts-export.lock"


def _source_snapshot(feedback_dir: Path) -> list[tuple[Path, bytes]]:
    """Read a complete, content-addressed view of the pin stores.

    The feedback routes archive by creating an archive file then replacing the
    active comments file. A directory listing alone is therefore not a stable
    read: an archive between listing and parsing would silently disappear from
    the ledger. The caller compares two snapshots and refuses on any change.
    """
    if not feedback_dir.is_dir():
        raise ValueError(f"feedback directory is unavailable: {feedback_dir}")
    active = feedback_dir / COMMENTS
    if not active.is_file():
        raise ValueError(f"active feedback source is unavailable: {active}")
    sources = [active, *sorted(feedback_dir.glob("archive-*.json"))]
    snapshot: list[tuple[Path, bytes]] = []
    for source in sources:
        try:
            payload = source.read_bytes()
        except OSError as exc:
            raise ValueError(f"cannot read feedback source {source}: {exc}") from exc
        snapshot.append((source, hashlib.sha256(payload).digest()))
    return snapshot


def _read_comments(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read feedback source {path}: {exc}") from exc
    comments = payload.get("comments") if isinstance(payload, dict) else None
    if not isinstance(comments, list):
        raise TypeError(f"feedback source {path} does not hold a 'comments' list")
    if not all(isinstance(comment, dict) for comment in comments):
        raise ValueError(f"feedback source {path} has a non-object comment")
    return comments


def _require_stable_sources(feedback_dir: Path, before: list[tuple[Path, bytes]]) -> None:
    """Refuse a projection that cannot speak for one stable feedback generation."""
    if _source_snapshot(feedback_dir) != before:
        raise ValueError(
            f"feedback source changed during export: {feedback_dir}; refusing incomplete pin ledger"
        )


def _validate_pin(source: Path, pin: dict[str, Any]) -> None:
    """Validate every persisted ``CommentOut`` field without hook dependencies."""
    required_strings = ("id", "page", "text", "created_at")
    required_nullable_strings = ("anchor",)
    optional_strings = ("status", "issue_url", "agent_note", "updated_at", "fixed_in_sha", "fixed_at", "harvested_at")
    for field in required_strings:
        if not isinstance(pin.get(field), str):
            raise TypeError(f"feedback source {source} has a pin with invalid {field}")
    for field in required_nullable_strings:
        if field not in pin or (pin[field] is not None and not isinstance(pin[field], str)):
            raise TypeError(f"feedback source {source} has a pin with invalid {field}")
    for field in optional_strings:
        if field in pin and pin[field] is not None and not isinstance(pin[field], str):
            raise TypeError(f"feedback source {source} has a pin with invalid {field}")
    for field in ("x_pct", "y_pct"):
        value = pin.get(field)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise TypeError(f"feedback source {source} has a pin with invalid {field}")
    replies = pin.get("replies")
    if replies is not None:
        if not isinstance(replies, list):
            raise TypeError(f"feedback source {source} has a pin with invalid replies")
        for reply in replies:
            if not isinstance(reply, dict):
                raise TypeError(f"feedback source {source} has a pin with invalid replies entry")
            for field in ("id", "author", "text", "created_at"):
                if not isinstance(reply.get(field), str):
                    raise TypeError(
                        f"feedback source {source} has a pin reply with invalid {field}"
                    )
    build = pin.get("build")
    if not isinstance(build, dict):
        raise TypeError(f"feedback source {source} has a pin with invalid build")
    for field in ("git_sha", "built_at_utc", "source", "error"):
        if field in build and build[field] is not None and not isinstance(build[field], str):
            raise TypeError(f"feedback source {source} has a pin with invalid build.{field}")


def read_pins(feedback_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Read every pin exactly once from the active store and immutable archives."""
    before = _source_snapshot(feedback_dir)
    pins: list[tuple[Path, dict[str, Any]]] = []
    seen: set[str] = set()
    for source, _digest in before:
        for pin in _read_comments(source):
            _validate_pin(source, pin)
            pin_id = pin["id"]
            if pin_id in seen:
                raise ValueError(
                    f"feedback source contains duplicate pin id {pin_id!r}; refusing to lose one"
                )
            seen.add(pin_id)
            pins.append((source, pin))
    _require_stable_sources(feedback_dir, before)
    return pins


def export_rows(feedback_dir: Path) -> list[dict[str, Any]]:
    """Map stored pins onto the common prompt fields while retaining pin provenance."""
    rows: list[dict[str, Any]] = []
    for source, pin in read_pins(feedback_dir):
        row = dict(pin)
        row.update({
            "tool": "opendj-feedback-pin",
            "ssid": pin["id"],
            "source": str(source),
            "at": pin["created_at"],
            "text": pin["text"],
        })
        rows.append(row)
    return sorted(rows, key=lambda row: (row["at"], row["ssid"]), reverse=True)


def exportable_rows(feedback_dir: Path) -> list[dict[str, Any]]:
    """Return rows only when the source can truthfully produce a pin ledger."""
    rows = export_rows(feedback_dir)
    if not rows:
        raise ValueError(f"feedback source contains no pins: {feedback_dir}; refusing empty export")
    return rows


def write_export(repo: Path, feedback_dir: Path, output: Path | None = None) -> Path:
    """Write a deterministic JSONL projection, atomically, without touching its source."""
    target = output or main_worktree(repo) / OUT_DIRNAME / LEDGER
    with sweep_lock(main_worktree(repo), lockfile=LOCKFILE, directory=target.parent) as acquired:
        if not acquired:
            raise ValueError(f"pin ledger is already being replaced: {target}")
        return _write_export(target, feedback_dir)


def _write_export(target: Path, feedback_dir: Path) -> Path:
    """Replace an already-locked pin projection after validating its source."""
    rows = exportable_rows(feedback_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            existing = [
                json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()
            ]
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read existing pin ledger {target}: {exc}") from exc
        existing_ids: set[str] = set()
        for row in existing:
            if not isinstance(row, dict) or not isinstance(row.get("ssid"), str):
                raise TypeError(f"existing pin ledger {target} has a row without string ssid")
            existing_ids.add(row["ssid"])
        fresh_ids = {row["ssid"] for row in rows}
        missing = existing_ids - fresh_ids
        if missing:
            raise ValueError(
                f"feedback source omits {len(missing)} pin(s) already in {target}; "
                "refusing regressing export"
            )
    content = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    if not target.exists() or target.read_text(encoding="utf-8") != content:
        _write_atomic(target, content)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--feedback-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None, help="explicit disposable output path")
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    if not (repo / ".git").exists():
        parser.error(f"not a git checkout: {repo}")
    output = args.output.resolve() if args.output else None
    try:
        print(write_export(repo, args.feedback_dir.resolve(), output))
    except (ValueError, TypeError, OSError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
