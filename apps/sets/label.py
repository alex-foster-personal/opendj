"""Hand-labeling CLI for recorded transitions.

Plan 12-02 Step 4. Walks a session's transitions and collects
user-provided class labels that the trained classifier consumes.
The labels file is append-only JSONL; last row per idx wins.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import paths as sets_paths
from .classify import CLASS_LIST, read_transitions

# Single-key prompts per Plan 12-02 Step 4.
_KEYMAP: dict[str, str | None] = {
    "c": "cut",
    "b": "blend",
    "f": "filter_sweep",
    "x": "fx",
    "q": "quick_double",
    "u": "unknown",
    "s": None,  # skip
    "": None,   # accept predicted
}


def labels_path(session_id: str, *, sets_root: Path | None = None) -> Path:
    root = Path(sets_root) if sets_root is not None else sets_paths.SETS_DIR
    return sets_paths.session_dir(session_id, root=root) / "labels.jsonl"


def read_labels(session_id: str, *, sets_root: Path | None = None) -> dict[int, str]:
    """Return ``{idx: latest_class}`` for a session; missing file -> empty."""
    path = labels_path(session_id, sets_root=sets_root)
    out: dict[int, str] = {}
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            out[int(row["idx"])] = str(row["class"])
    return out


def append_label(
    session_id: str,
    idx: int,
    class_: str,
    *,
    labeler: str = "cli",
    sets_root: Path | None = None,
    now: datetime | None = None,
) -> Path:
    """Append one label row to ``labels.jsonl``."""
    if class_ not in CLASS_LIST:
        raise ValueError(f"unknown class {class_!r}; expected one of {CLASS_LIST}")
    path = labels_path(session_id, sets_root=sets_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).isoformat(timespec="milliseconds")
    row = {
        "idx": int(idx),
        "class": class_,
        "labeler": labeler,
        "labeled_at": stamp,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, separators=(",", ":"), sort_keys=True))
        fh.write("\n")
    return path


def _format_prompt(transition: dict[str, Any]) -> str:
    feats = transition.get("features", {})
    return (
        f"[{transition['idx']}] {transition.get('from_track')} -> "
        f"{transition.get('to_track')}  "
        f"overlap={feats.get('overlap_s', 0):.1f}s "
        f"fade={feats.get('fade_s', 0):.1f}s  "
        f"predicted={transition.get('predicted_class')}"
        f"({transition.get('confidence', 0):.2f})  "
        f"[c]ut [b]lend [f]ilter [x]fx [q]uick-double [u]nknown [s]kip "
        f"[enter=accept predicted]"
    )


def label_session(
    session_id: str,
    *,
    sets_root: Path | None = None,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
    relabel: bool = False,
    labeler: str = "cli",
    transitions: list[dict[str, Any]] | None = None,
) -> dict[int, str]:
    """Interactively label every transition in ``session_id``.

    Returns the final ``{idx: class}`` mapping. ``input_fn`` is
    injectable for testing.
    """
    if transitions is None:
        transitions = read_transitions(session_id, sets_root=sets_root)
    existing = read_labels(session_id, sets_root=sets_root)
    if not transitions:
        print_fn(f"no transitions for {session_id}")
        return existing

    any_work = False
    for t in transitions:
        idx = int(t["idx"])
        if idx in existing and not relabel:
            continue
        any_work = True
        prompt = _format_prompt(t)
        print_fn(prompt)
        while True:
            raw = input_fn("> ").strip().lower()
            if raw not in _KEYMAP:
                print_fn(f"unrecognised key {raw!r}; try again")
                continue
            action = _KEYMAP[raw]
            if raw == "s":
                break  # skip -- no label written
            if raw == "":
                action = t.get("predicted_class")
                if action not in CLASS_LIST:
                    action = "unknown"
            append_label(
                session_id,
                idx,
                action,
                labeler=labeler,
                sets_root=sets_root,
            )
            existing[idx] = action
            break

    if not any_work:
        print_fn("all transitions already labeled; use --relabel to overwrite")
    return existing


__all__ = [
    "labels_path",
    "read_labels",
    "append_label",
    "label_session",
]
