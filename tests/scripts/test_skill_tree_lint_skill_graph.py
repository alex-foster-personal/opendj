"""Skill-to-skill reference graph and markdown fence handling.

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


def test_skills_naming_skills_are_measured_as_a_chain(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _skill(repo, "top-level-skill", "Hand off to [[middle-skill-b]].")
    _skill(repo, "middle-skill-b", "Then run the leaf-skill-c skill.")
    _skill(repo, "leaf-skill-c", "Does the work.")
    report = measure(repo)
    assert report.skill_ref_depth_max == 2
    assert report.skill_ref_worst == ["top-level-skill", "middle-skill-b", "leaf-skill-c"]
    assert report.skill_ref_cycles == []


def test_two_trees_holding_one_skill_name_both_keep_their_references(tmp_path: Path) -> None:
    # Codex P2 on #3846: keying the graph by name let the later tree's copy
    # overwrite the earlier one, so a chain in the hidden copy scored depth 0.
    repo = _repo(tmp_path)
    _skill(repo, "leaf-skill-c", "Does the work.")
    _skill(repo, "dup-skill", "Hand off to [[leaf-skill-c]].")
    _skill(repo, "dup-skill", "No references.", tree="(private path)")
    report = measure(repo)
    assert report.skills_scanned == CFG.MIN_SKILLS + 3
    assert report.skill_ref_depth_max == 1
    assert report.skill_ref_worst == ["(private path)", "leaf-skill-c"]

    # Control: once neither copy names the leaf, nothing is left to measure.
    _skill(repo, "dup-skill", "No references.")
    assert measure(repo).skill_ref_depth_max == 0


def test_a_path_reference_points_at_its_own_copy_of_a_shared_name(tmp_path: Path) -> None:
    # Codex P2 on #3846: a path to one tree's copy also linked every other copy
    # of that name, so an unrelated copy referring back invented a cycle.
    repo = _repo(tmp_path)
    _skill(repo, "caller-skill", "Use the [local](../dup-skill/SKILL.md) one.")
    _skill(repo, "dup-skill", "No references.")
    _skill(repo, "dup-skill", "Hand back to [[caller-skill]].", tree="(private path)")
    graph = skill_graph(discover(repo), repo)
    assert graph["caller-skill"] == {"(private path)"}
    report = measure(repo)
    assert report.skill_ref_cycles == []
    assert report.cyclic_skill_refs == 0

    # Codex P2 BLOCKING on #3846: a label that restates the skill's name is
    # still a link to one copy, not a mention of every copy.
    _skill(repo, "caller-skill", "Use the [dup-skill](../dup-skill/SKILL.md) one.")
    assert skill_graph(discover(repo), repo)["caller-skill"] == {"(private path)"}
    # Codex P2 BLOCKING on #3846: a raw HTML anchor pairs its label the same way.
    for body in (
        'Use the <a href="../dup-skill/SKILL.md">dup-skill</a> one.',
        '<p>Use the <a href="../dup-skill/SKILL.md">dup-skill</a> one.</p>',
        # So does a code span in a label, read apart from the label's words.
        "Use the [`skills/dup-skill`](../dup-skill/SKILL.md) one.",
    ):
        _skill(repo, "caller-skill", body)
        assert skill_graph(discover(repo), repo)["caller-skill"] == {"(private path)"}, body
    assert measure(repo).skill_ref_cycles == []
    # Control: a block anchor's `</a>` ends its label, so a name after it is
    # prose again and names every copy.
    _skill(repo, "caller-skill", '<p>Use the <a href="../dup-skill/SKILL.md">local</a> one, not any dup-skill.</p>')
    graph = skill_graph(discover(repo), repo)
    assert graph["caller-skill"] == {"(private path)", "(private path)"}

    # Control: the same label on a link to something that is not a skill still
    # names the skill, and so every copy of it.
    (repo / "(private path)").write_text("Notes.\n", encoding="utf-8")
    _skill(repo, "caller-skill", "Use [dup-skill](notes.md) first.")
    graph = skill_graph(discover(repo), repo)
    assert graph["caller-skill"] == {"(private path)", "(private path)"}

    # Control: a bare wiki link cannot say which copy it means, so it names both.
    _skill(repo, "caller-skill", "Use [[dup-skill]].")
    graph = skill_graph(discover(repo), repo)
    assert graph["caller-skill"] == {"(private path)", "(private path)"}
    assert measure(repo).skill_ref_cycles == ["(private path) <-> caller-skill"]


def test_a_path_or_label_names_only_what_it_says(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _skill(repo, "first-skill", "Does the work.")
    _skill(repo, "other-skill", "Does the work.")
    _skill(repo, "target-skill", "Does the work.")

    # Codex P2 BLOCKING on #3846: `../target-skill.md` is a file beside the
    # skill directory, not the skill.
    (repo / "(private path)").write_text("A note.\n", encoding="utf-8")
    _skill(repo, "caller-skill", "Read the [note](../target-skill.md).")
    assert skill_graph(discover(repo), repo)["caller-skill"] == set()
    _skill(repo, "caller-skill", "Read the [note](../target-skill/SKILL.md).")
    assert skill_graph(discover(repo), repo)["caller-skill"] == {"target-skill"}

    # Codex P2 BLOCKING on #3846: a label on a resolved skill link can still
    # invoke a second skill, and that edge counts.
    _skill(repo, "caller-skill", "Run [first, then /other-skill](../first-skill/SKILL.md).")
    assert skill_graph(discover(repo), repo)["caller-skill"] == {"first-skill", "other-skill"}
    # Codex P2 BLOCKING on #3846: `skill:<name>` is an explicit handoff, even
    # for a short name the bare-name fallback skips.
    _skill(repo, "html", "Does the work.")
    _skill(repo, "caller-skill", "Then hand off to skill:html.")
    assert skill_graph(discover(repo), repo)["caller-skill"] == {"html"}
    # Codex P2 BLOCKING on #3846: a URI scheme is case-insensitive.
    for body in ("Then [run](SKILL:html).", "Then hand off to Skill:html.", '<a href="SKILL:html">go</a>'):
        _skill(repo, "caller-skill", body)
        assert skill_graph(discover(repo), repo)["caller-skill"] == {"html"}, body
    # Codex P2 BLOCKING on #3846: `skill:<own-name>` is a self-handoff, a loop.
    _skill(repo, "html", "Retry with skill:html.")
    assert measure(repo).cyclic_skill_refs >= 1
    _skill(repo, "html", "Does the work.")
    _skill(repo, "caller-skill", "Then write the html.")
    assert skill_graph(discover(repo), repo)["caller-skill"] == set()

    # Control: a label that only restates its destination adds nothing more.
    _skill(repo, "caller-skill", "Run [first-skill](../first-skill/SKILL.md).")
    assert skill_graph(discover(repo), repo)["caller-skill"] == {"first-skill"}
    # Sol P1 BLOCKING on #3846: a segment the destination climbs back out of
    # is not where it points, so a label naming that segment is a handoff.
    _skill(repo, "caller-skill", "Run [target-skill](../target-skill/../other-skill/SKILL.md).")
    assert skill_graph(discover(repo), repo)["caller-skill"] == {"other-skill", "target-skill"}


def test_a_bare_md_name_stops_at_the_extension(tmp_path: Path) -> None:
    # Codex P2 BLOCKING on #3846: `guide.md.bak` and `guide.md-old` matched
    # `guide.md`, importing an unrelated file's depth.
    repo = _repo(tmp_path)
    root = _skill(repo, "doc-skill", "Old copies: guide.md.bak and guide.md-old.")
    (root / "guide.md").write_text("Then deeper.md.\n", encoding="utf-8")
    (root / "deeper.md").write_text("Done.\n", encoding="utf-8")
    assert measure(repo).file_ref_depth_max == 0

    # Codex P2 BLOCKING on #3846: an unused reference definition is not a link.
    _skill(repo, "doc-skill", "Nothing here.\n\n[old]: guide.md")
    assert measure(repo).file_ref_depth_max == 0
    _skill(repo, "doc-skill", "Read the [guide][old].\n\n[old]: guide.md")
    assert measure(repo).file_ref_depth_max == 2

    # Control: the real name, sentence punctuation and all, is still followed.
    _skill(repo, "doc-skill", "Read guide.md.")
    assert measure(repo).file_ref_depth_max == 2


def test_an_unlinked_note_in_a_skill_adds_no_skill_reference(tmp_path: Path) -> None:
    # Codex P2 on #3846: an agent is only led to SKILL.md and the files it
    # reaches, so a note nothing links must not add edges, depth or cycles.
    repo = _repo(tmp_path)
    root = _skill(repo, "noting-skill", "Plain instructions.")
    _skill(repo, "target-skill", "Does the work.")
    (root / "UNLINKED.md").write_text("Hand off to [[target-skill]].\n", encoding="utf-8")
    assert skill_graph(discover(repo), repo)["noting-skill"] == set()

    # Control: the same note, linked from SKILL.md, is read and counts.
    (root / "SKILL.md").write_text("---\nname: noting-skill\n---\nSee [note](UNLINKED.md).\n", encoding="utf-8")
    assert skill_graph(discover(repo), repo)["noting-skill"] == {"target-skill"}


def test_a_sibling_skill_link_with_an_unrelated_label_is_a_reference(tmp_path: Path) -> None:
    # Codex P2 on #3846: [specialist](../html/SKILL.md) matched no pattern, so
    # a three-skill chain built from ordinary relative links scored depth 0.
    repo = _repo(tmp_path)
    _skill(repo, "docs", "Hand off to the [specialist](../html/SKILL.md).")
    _skill(repo, "html", "Then the [next step](../pdf/SKILL.md).")
    _skill(repo, "pdf", "Does the work.")
    report = measure(repo)
    assert report.skill_ref_depth_max == 2
    assert report.skill_ref_worst == ["docs", "html", "pdf"]

    # Control: a longer sibling name that merely starts with "html" is not "html".
    _skill(repo, "docs", "Hand off to the [specialist](../html-legacy/SKILL.md).")
    assert measure(repo).skill_ref_worst[0] != "docs"

    # Codex P2 BLOCKING on #3846: a percent-encoded or angle-bracketed
    # destination names the same sibling once the link is parsed and decoded.
    for body in (
        "Hand off to the [specialist](../ht%6Dl/SKILL.md).",
        "Hand off to the [specialist](<../html/SKILL.md>).",
        "Hand off to the [specialist][s].\n\n[s]: ../h%74ml/SKILL.md",
    ):
        _skill(repo, "docs", body)
        assert measure(repo).skill_ref_worst == ["docs", "html", "pdf"], body


def test_a_link_into_a_nested_skill_directory_is_a_reference(tmp_path: Path) -> None:
    # Codex P2 BLOCKING on #3846: a skill one directory further down, such as
    # the exporter's `_vendored/pdf`, matched no path pattern, and a short name
    # under a descriptive label cannot fall back to a bare-name match.
    repo = _repo(tmp_path)
    _skill(repo, "pdf", "Does the work.", tree="(private path)")
    for body in (
        "Hand off to the [exporter output](../_vendored/pdf/SKILL.md).",
        "Then see (private path).",
    ):
        _skill(repo, "docs", body)
        assert skill_graph(discover(repo), repo)["docs"] == {"pdf"}, body

    # Codex P2 BLOCKING on #3846: a skill nested directly inside another is
    # linked as a direct child, with no parent directory before its name.
    _skill(repo, "nesting-skill", "Hand off to the [specialist](lint/SKILL.md).")
    _skill(repo, "lint", "Does the work.", tree="(private path)")
    assert skill_graph(discover(repo), repo)["nesting-skill"] == {"lint"}
    # Codex P2 BLOCKING on #3846: a link destination is a path even with no
    # `/` in it.
    for body in ("Hand off to the [specialist](lint).", "Hand off to the [specialist](lint#usage)."):
        _skill(repo, "nesting-skill", body)
        assert skill_graph(discover(repo), repo)["nesting-skill"] == {"lint"}, body
    # Codex P2 BLOCKING on #3846: a destination is resolved whole, whatever
    # its parent directories are called, and a file inside a skill leads to it.
    _skill(repo, "spdf", "Does the work.", tree="(private path) folder")
    for body in (
        "Hand off to the [specialist](<../some folder/spdf/SKILL.md>).",
        "Hand off to the [specialist](../some%20folder/spdf/SKILL.md).",
        "Read the [reference](<../some folder/spdf/reference.md>).",
        '<p>Hand off to the <a href="../some folder/spdf/">specialist</a>.</p>',
        # Codex P2 BLOCKING on #3846: an anchored path in a code span is
        # taken whole, spaces and all.
        "Hand off to `../some folder/spdf/SKILL.md`.",
        "Hand off to `../some folder/spdf`, then stop.",
        "Hand off to `../some%20folder/spdf/SKILL.md`.",
        "Hand off to `(private path) folder/spdf/SKILL.md`.",
        # Codex P2 BLOCKING on #3846: so is a quoted path in prose.
        'Hand off to "../some folder/spdf/SKILL.md".',
        "Hand off to \u201c../some folder/spdf\u201d, then stop.",
        'Hand off to "(private path) folder/spdf/SKILL.md".',
        "Hand off to '../some folder/spdf/SKILL.md'.",
        "Hand off to \u2018../some folder/spdf\u2019, then stop.",
    ):
        _skill(repo, "docs", body)
        assert skill_graph(discover(repo), repo)["docs"] == {"spdf"}, body
    # Control: code is literal, so a `%20` that is part of a real directory
    # name still resolves as written.
    _skill(repo, "lit-skill", "Does the work.", tree="(private path)%20dir")
    _skill(repo, "docs", "Hand off to `../odd%20dir/lit-skill/SKILL.md`.")
    assert skill_graph(discover(repo), repo)["docs"] == {"lit-skill"}
    # A label restating the name of a skill linked that way still names only
    # the copy it links to.
    _skill(repo, "far-skill", "Does the work.", tree="(private path) folder")
    _skill(repo, "far-skill", "Does the work.", tree="(private path)")
    _skill(repo, "docs", "Hand off to [far-skill](<../some folder/far-skill/SKILL.md>).")
    assert skill_graph(discover(repo), repo)["docs"] == {"(private path) folder/far-skill"}
    # Control: a link to a skill's own file is not a reference to itself.
    _skill(repo, "docs", "Read the [notes](reference.md) and [this](SKILL.md).")
    assert skill_graph(discover(repo), repo)["docs"] == set()
    # Control: the bare word is not a path, so it names no skill.
    _skill(repo, "nesting-skill", "Then lint the file.")
    assert skill_graph(discover(repo), repo)["nesting-skill"] == set()

    # Control: a path that names the skill but resolves to no skill directory
    # is not a reference to it.
    for body in (
        "See the [notes](../_archive/pdf/SKILL.md).",
        "Then see docs/pdf/README.md.",
    ):
        _skill(repo, "docs", body)
        assert skill_graph(discover(repo), repo)["docs"] == set(), body


def test_a_nested_skill_is_not_part_of_its_parents_files(tmp_path: Path) -> None:
    # Codex P2 BLOCKING on #3846: a subskill linked one level down was scored
    # as part of its parent's file chain, and its handoffs as the parent's.
    repo = _repo(tmp_path)
    _skill(repo, "parent-skill", "Hand off to the [linter](lint/SKILL.md).")
    lint = _skill(
        repo, "lint", "Read [the rules](rules.md), then skill:other-skill.", tree="(private path)"
    )
    (lint / "rules.md").write_text("The rules.\n", encoding="utf-8")
    _skill(repo, "other-skill", "Does the work.")
    by_name = {s.name: s for s in discover(repo)}
    assert file_ref_depth(by_name["parent-skill"], repo) == [by_name["parent-skill"].skill_md.resolve()]
    assert file_ref_depth(by_name["lint"], repo) == [lint.resolve() / "SKILL.md", lint.resolve() / "rules.md"]
    assert skill_graph(discover(repo), repo)["parent-skill"] == {"lint"}
    # Control: the parent's own file one level down is still its file.
    parent = by_name["parent-skill"].root
    (parent / "guide.md").write_text("A guide.\n", encoding="utf-8")
    _skill(repo, "parent-skill", "Read the [guide](guide.md), then the [linter](lint/SKILL.md).")
    assert file_ref_depth(by_name["parent-skill"], repo) == [parent / "SKILL.md", parent / "guide.md"]


def test_a_name_inside_a_fenced_example_is_not_a_reference(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _skill(repo, "doc-example-skill", "```\n[[leaf-skill-c]]\n```")
    _skill(repo, "leaf-skill-c", "Does the work.")
    assert measure(repo).skill_ref_depth_max == 0

    # Mutation: the same name outside the fence is a reference.
    _skill(repo, "doc-example-skill", "[[leaf-skill-c]]")
    assert measure(repo).skill_ref_depth_max == 1


def test_a_fence_closes_only_on_its_own_marker(tmp_path: Path) -> None:
    # Codex P2 BLOCKING on #3846: a `~~~` line (or a shorter backtick run)
    # inside a backtick fence toggled the filter off, exposing the example.
    repo = _repo(tmp_path)
    _skill(repo, "leaf-skill-c", "Does the work.")
    for body in (
        "```\n~~~\n[[leaf-skill-c]]\n```",
        "````\n```\n[[leaf-skill-c]]\n````",
        "~~~\n```\n[[leaf-skill-c]]\n~~~",
        "```\n``` not a close\n[[leaf-skill-c]]\n```",
        # Codex P2 BLOCKING on #3846: a fence inside a block quote or list item
        # never starts its raw line, so only the parser can see it.
        "> ```\n> [[leaf-skill-c]]\n> ```",
        "- Example:\n\n  ```\n  [[leaf-skill-c]]\n  ```",
        "Indented example:\n\n    [[leaf-skill-c]]",
        # Codex P2 BLOCKING on #3846: a run indented four columns is content of
        # the open fence, not its close.
        "```\n    ```\n[[leaf-skill-c]]\n```",
        # Codex P2 BLOCKING on #3846: the three-column limit on a close is
        # absolute, not relative to an indented opener.
        "   ```\n      ```\n[[leaf-skill-c]]\n   ```",
        # Codex P2 BLOCKING on #3846: a fence in a list item indented four
        # columns is still a fence, because the indent belongs to the item.
        "- Example:\n\n    ```\n    [[leaf-skill-c]]\n    ```",
        "1. Example:\n\n   > ```\n   > [[leaf-skill-c]]\n   > ```",
        # Codex P2 BLOCKING on #3846: an HTML comment is invisible.
        "Old step: <!-- [[leaf-skill-c]] -->",
        "<!--\nRetired: [[leaf-skill-c]]\n-->",
        # Codex P2 BLOCKING on #3846: an image's alt text describes a picture.
        "![leaf-skill-c](diagram.png)",
        "![Flow into [[leaf-skill-c]]](diagram.png)",
    ):
        _skill(repo, "doc-example-skill", body)
        assert measure(repo).skill_ref_depth_max == 0, body

    # Control: past the real closing fence the same name is a reference.
    _skill(repo, "doc-example-skill", "```\n~~~\n```\n[[leaf-skill-c]]")
    assert measure(repo).skill_ref_depth_max == 1
    # Control: a run indented four columns opens no fence, so it hides nothing
    # after it.
    _skill(repo, "doc-example-skill", "Indented:\n\n    ```\n\n[[leaf-skill-c]]")
    assert measure(repo).skill_ref_depth_max == 1
    # Control: text after an HTML comment is still read.
    _skill(repo, "doc-example-skill", "<!-- retired --> Then [[leaf-skill-c]].")
    assert measure(repo).skill_ref_depth_max == 1
    # Control: text after an image is still read.
    _skill(repo, "doc-example-skill", "![diagram](diagram.png) Then [[leaf-skill-c]].")
    assert measure(repo).skill_ref_depth_max == 1
    # Control: a fence inside a list item closes at the item's own indent.
    _skill(repo, "doc-example-skill", "- Example:\n\n  ```\n  x\n  ```\n\n[[leaf-skill-c]]")
    assert measure(repo).skill_ref_depth_max == 1
