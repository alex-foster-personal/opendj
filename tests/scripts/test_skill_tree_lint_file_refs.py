"""File-reference depth and SKILL line-limit measurements.

Sibling of ``test_skill_tree_lint.py`` for ``scripts/skill_tree_lint.py``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import SplitResult

import pytest

from scripts import skill_tree_lint
from scripts.skill_tree_lint import (
    UnparsableDestinationError,
    discover,
    file_ref_depth,
    main,
    measure,
    skill_graph,
)

from tests.scripts.skill_tree_lint_support import CFG, REPO, _git, _repo, _skill


def test_a_skill_at_the_line_limit_is_counted_and_one_under_it_is_not(tmp_path: Path) -> None:
    # Codex P2 on #3846: the rule says "under 500", so 500 lines already breaks it.
    repo = _repo(tmp_path)
    under = _skill(repo, "at-limit-skill")
    (under / "SKILL.md").write_text("x\n" * (CFG.SKILL_MAX_LINES - 1), encoding="utf-8")
    report = measure(repo)
    assert report.over_line_limit == []
    assert report.over_line_excess == 0

    (under / "SKILL.md").write_text("x\n" * CFG.SKILL_MAX_LINES, encoding="utf-8")
    report = measure(repo)
    assert report.over_line_limit == [f"at-limit-skill ({CFG.SKILL_MAX_LINES} lines)"]
    assert report.over_line_excess == 1


def test_a_chain_of_reference_files_is_deeper_than_direct_links(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    root = _skill(repo, "chained-skill", "See [advanced](advanced.md).")
    (root / "advanced.md").write_text("See [details](details.md).\n", encoding="utf-8")
    (root / "details.md").write_text("The actual information.\n", encoding="utf-8")
    report = measure(repo)
    assert report.file_ref_depth_max == 2
    assert report.file_ref_worst[-1].endswith("chained-skill/details.md")

    # Codex P2 BLOCKING on #3846: a malformed remote URL does not abort the scan.
    # Sol P1 BLOCKING on #3846: that is because its authority makes it remote,
    # not because a parse error is swallowed (see the next test).
    _skill(repo, "broken-link-skill", '[docs](http://[bad) and <a href="http://[bad">docs</a>.')
    assert measure(repo).file_ref_depth_max == 2
    # Control: a fragment after a markdown file name is not a host's.
    (root / "SKILL.md").write_text("See advanced.md#usage first.\n", encoding="utf-8")
    assert measure(repo).file_ref_depth_max == 2
    # Codex P2 BLOCKING on #3846: a delimited path's fragment is not its file.
    for body in ("See `./advanced.md#usage` first.", 'See "./advanced.md?x=1#usage" first.'):
        (root / "SKILL.md").write_text(body + "\n", encoding="utf-8")
        assert measure(repo).file_ref_depth_max == 2, body

    # Control: the same three files, both linked straight from SKILL.md.
    (root / "SKILL.md").write_text("See [a](advanced.md) and [d](details.md).\n", encoding="utf-8")
    (root / "advanced.md").write_text("No further links.\n", encoding="utf-8")
    assert measure(repo).file_ref_depth_max == 1


def test_a_link_destination_with_spaces_is_followed_whole(tmp_path: Path) -> None:
    # Codex P2 on #3846: `[guide](<reference guide.md>)` was read as `guide.md`,
    # so the linked file, its depth and its skill handoffs were never seen.
    repo = _repo(tmp_path)
    root = _skill(repo, "spaced-skill", "See [guide](<reference guide.md>).")
    _skill(repo, "handoff-skill", "Does the work.")
    (root / "reference guide.md").write_text("See [deep](deep.md). Then [[handoff-skill]].\n", encoding="utf-8")
    (root / "deep.md").write_text("The actual information.\n", encoding="utf-8")
    assert measure(repo).file_ref_depth_max == 2
    assert skill_graph(discover(repo), repo)["spaced-skill"] == {"handoff-skill"}

    # The percent-encoded form names the same file.
    (root / "SKILL.md").write_text(
        "---\nname: spaced-skill\n---\nSee [guide](reference%20guide.md).\n", encoding="utf-8"
    )
    assert measure(repo).file_ref_depth_max == 2

    # Codex P2 on #3846: a sibling file named like a fragment of that
    # destination (`guide.md` inside `reference guide.md`) is not linked by it.
    (root / "SKILL.md").write_text(
        "---\nname: spaced-skill\n---\nSee [guide](<reference guide.md>).\n", encoding="utf-8"
    )
    (root / "reference guide.md").write_text("No further links.\n", encoding="utf-8")
    (root / "guide.md").write_text("See [deep](deep.md).\n", encoding="utf-8")
    assert measure(repo).file_ref_depth_max == 1

    # Control: a link to a file that does not exist reaches nothing.
    (root / "SKILL.md").write_text("---\nname: spaced-skill\n---\nSee [guide](<missing guide.md>).\n", encoding="utf-8")
    assert skill_graph(discover(repo), repo)["spaced-skill"] == set()


def test_a_link_destination_with_parentheses_is_followed_whole(tmp_path: Path) -> None:
    # Codex P2 BLOCKING on #3846: CommonMark allows balanced or escaped
    # parentheses in a bare destination, and `reference_(advanced).md` was
    # dropped, so the chain through it scored depth 0.
    repo = _repo(tmp_path)
    _skill(repo, "handoff-skill", "Does the work.")
    root = _skill(repo, "paren-skill", "See [g](reference_(advanced).md).")
    (root / "reference_(advanced).md").write_text("See [deep](deep.md). Then [[handoff-skill]].\n", encoding="utf-8")
    (root / "deep.md").write_text("The actual information.\n", encoding="utf-8")
    for body in (
        "See [g](reference_(advanced).md).",
        "See [g](reference_\\(advanced\\).md).",
        "See [g](reference_(advanced).md#usage).",
        'See [g][g].\n\n[g]: reference_(advanced).md "a title"',
    ):
        (root / "SKILL.md").write_text(f"---\nname: paren-skill\n---\n{body}\n", encoding="utf-8")
        assert measure(repo).file_ref_depth_max == 2, body
        assert skill_graph(discover(repo), repo)["paren-skill"] == {"handoff-skill"}, body

    # Codex P2 BLOCKING on #3846: CommonMark nests parentheses without limit,
    # which is why links are parsed, not pattern-matched.
    (root / "reference_((advanced)).md").write_text("See [deep](deep.md).\n", encoding="utf-8")
    (root / "SKILL.md").write_text(
        "---\nname: paren-skill\n---\nSee [g](reference_((advanced)).md).\n", encoding="utf-8"
    )
    assert measure(repo).file_ref_depth_max == 2

    # Control: a destination that names no file on disk reaches nothing.
    (root / "SKILL.md").write_text("---\nname: paren-skill\n---\nSee [g](reference_(basic).md).\n", encoding="utf-8")
    assert measure(repo).file_ref_depth_max == 0
    assert skill_graph(discover(repo), repo)["paren-skill"] == set()


def test_a_filename_in_a_link_title_is_not_a_reference(tmp_path: Path) -> None:
    # Codex P2 BLOCKING on #3846: `[t](target.md "see orphan.md")` read the
    # title as a second link, pulling an unlinked file into depth and handoffs.
    repo = _repo(tmp_path)
    _skill(repo, "handoff-skill", "Does the work.")
    root = _skill(repo, "titled-skill", 'See [t](target.md "see orphan.md").')
    (root / "target.md").write_text("No further links.\n", encoding="utf-8")
    (root / "orphan.md").write_text("See [deep](deep.md). Then [[handoff-skill]].\n", encoding="utf-8")
    (root / "deep.md").write_text("The actual information.\n", encoding="utf-8")
    for body in (
        'See [t](target.md "see orphan.md").',
        "See [t](target.md 'see orphan.md').",
        "See [t](<target.md> (see orphan.md)).",
        'See [t][t].\n\n[t]: target.md "see orphan.md"',
        # Codex P2 BLOCKING on #3846: a title may start on the next line or
        # span lines, and is still part of the one link.
        'See [t](target.md\n  "see orphan.md").',
        "See [t](target.md 'a title that\ngoes on to orphan.md').",
        "See [t].\n\n[t]: <target.md>\n  (see orphan.md)",
        # Codex P2 BLOCKING on #3846: a skill named only in a title is not a
        # handoff either.
        'See [t](target.md "handoff-skill").',
    ):
        (root / "SKILL.md").write_text(f"---\nname: titled-skill\n---\n{body}\n", encoding="utf-8")
        assert measure(repo).file_ref_depth_max == 1, body
        assert skill_graph(discover(repo), repo)["titled-skill"] == set(), body

    # Control: the same filename named in prose outside a link is still read.
    (root / "SKILL.md").write_text(
        '---\nname: titled-skill\n---\nSee [t](target.md "a title"), then orphan.md.\n', encoding="utf-8"
    )
    assert measure(repo).file_ref_depth_max == 2
    assert skill_graph(discover(repo), repo)["titled-skill"] == {"handoff-skill"}

    # Control: a blank line ends a title, so a quoted mention in the next
    # paragraph is prose again and is read.
    (root / "SKILL.md").write_text(
        '---\nname: titled-skill\n---\nSee [t](target.md "a title\n\nthen orphan.md").\n', encoding="utf-8"
    )
    assert measure(repo).file_ref_depth_max == 2


def test_a_file_outside_the_skill_does_not_count_as_depth(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("See [more](more.md).\n", encoding="utf-8")
    _skill(repo, "pointer-skill", "Read `docs/guide.md` first.")
    assert measure(repo).file_ref_depth_max == 0


def test_an_absolute_path_outside_the_skill_is_never_touched(tmp_path: Path) -> None:
    # CI hit this: a skill quoting a home-directory path made the lint stat a
    # directory the runner cannot read. A name too long for the filesystem
    # makes stat raise even as root, so the probe reproduces it anywhere.
    unreadable = "/" + "a" * 300 + "/SKILL.md"
    with pytest.raises(OSError):
        Path(unreadable).is_file()
    repo = _repo(tmp_path)
    _skill(repo, "quoting-skill", f"Once lived at `{unreadable}`.")
    assert measure(repo).file_ref_depth_max == 0
