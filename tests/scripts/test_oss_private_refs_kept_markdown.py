"""[if] kept docs or root markdown names a withheld path [then] publish gate fails, [else stop]."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.oss_private_refs import private_refs, tracked_files
from tests.scripts.oss_publish_tracked import is_kept_markdown, publish_tracked

pytestmark = pytest.mark.requirement("OSSPUB-12792")

REPO = Path(__file__).resolve().parents[2]


def _kept_findings() -> list[tuple[str, int, str]]:
    tracked = publish_tracked(tracked_files(REPO))
    found: list[tuple[str, int, str]] = []
    for rel in sorted(tracked):
        if not is_kept_markdown(rel):
            continue
        path = REPO / rel
        if not path.is_file():
            continue
        raw = path.read_bytes()
        if b"\0" in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for match in private_refs(text, tracked):
            found.append((rel, text.count("\n", 0, match.start()) + 1, match.group()))
    return found


@pytest.fixture(scope="module")
def kept_findings() -> list[tuple[str, int, str]]:
    return _kept_findings()


def test_kept_markdown_names_no_withheld_specs_app_docs_threads_or_planning(kept_findings: list) -> None:
    """[if] kept markdown names specs app_docs threads or unkept planning [then] fail, [else stop]."""
    assert not kept_findings, "\n".join(f"{p}:{ln}: {ref}" for p, ln, ref in kept_findings[:40])


def test_kept_markdown_has_no_github_urls_to_withheld_paths(kept_findings: list) -> None:
    """[if] a blob tree raw or edit URL names a withheld path [then] fail, [else stop]."""
    bad = [row for row in kept_findings if "/blob/" in row[2] or "/tree/" in row[2] or "/raw/" in row[2]]
    assert not bad


def test_kept_markdown_has_no_trailing_slash_withheld_directories(kept_findings: list) -> None:
    """[if] a trailing-slash withheld directory remains in kept markdown [then] fail, [else stop]."""
    bad = [row for row in kept_findings if row[2].rstrip().endswith(("specs/", "app_docs/", "docs/threads/"))]
    assert not bad


def test_oss_private_refs_check_scope_is_clean_under_publish_tracked(kept_findings: list) -> None:
    """[if] publish-tracked scan finds any kept-markdown private ref [then] fail, [else stop]."""
    assert kept_findings == []
