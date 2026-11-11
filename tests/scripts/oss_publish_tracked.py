"""Tracked paths as the published mirror ships them (for oss_private_refs doc gates)."""

from __future__ import annotations

WITHHELD_PREFIX = ("specs/", "app_docs/", "docs/threads/", "handoffs/")
WITHHELD_PLANNING = (
    ".planning/MAINTAINER-QUEUE.md",
    ".planning/MERGE-QUEUE.md",
    ".planning/roformer-spike-plan.md",
    ".planning/bifrost2-handoff",
    ".planning/cloudsync-levels-map-2026-09-11.md",
    ".planning/b2b-shared-booth/",
    ".planning/todos/",
    ".planning/strategy-brain/",
    ".planning/seeds/",
    "(private path)/",
)


def publish_tracked(tracked: frozenset[str]) -> frozenset[str]:
    out: set[str] = set()
    for path in tracked:
        if path.startswith(WITHHELD_PREFIX):
            continue
        if any(path == item.rstrip("/") or path.startswith(item) for item in WITHHELD_PLANNING):
            continue
        out.add(path)
    return frozenset(out)


def is_kept_markdown(rel: str) -> bool:
    if not rel.endswith(".md"):
        return False
    if rel.startswith("docs/threads/"):
        return False
    if rel == "FILETREE.md":
        # Private-checkout inventory: test_filetree.py requires naming withheld trees here.
        return False
    return rel.startswith("docs/") or "/" not in rel
