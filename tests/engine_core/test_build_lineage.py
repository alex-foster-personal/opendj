"""Build lineage colours and descent, against real git history (INSTALL-44).

Each test builds a throwaway repo in tmp_path with a trunk and two long-lived
lines, then asks the same functions the payload builder calls. No git is mocked.

Regression lines:
- if a build carrying af--set-rc and af--b2b-link is not teal then the badge
  cannot tell a mixed build from either line
- if a line merged into main still colours main then the blend never ends
- if a genre-keys build over a set-rc build reads as descended then the set
  fixes vanish silently again (Tue 3 Nov 2026)
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from apps.engine_core.build_lineage import (
    LINEAGE_CONFIG_PATH,
    LineageConfig,
    LineageError,
    LineSpec,
    compute_lineage,
    lineage_color,
    load_lineage_config,
    not_descended_warning,
)

CONFIG = LineageConfig(
    trunk="main",
    lines=(
        LineSpec("main", "refs/heads/main", "white"),
        LineSpec("af--set-rc", "refs/heads/af--set-rc", "cerulean"),
        LineSpec("af--b2b-link", "refs/heads/af--b2b-link", "yellow"),
        LineSpec("af--gone", "refs/heads/af--gone", "red"),
    ),
    palette=load_lineage_config().palette,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """main: m1 m2; af--set-rc: +s1 s2 s3; af--b2b-link: +b1 b2; af--genre-keys: main + g1."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _commit(tmp_path, "m1")
    _commit(tmp_path, "m2")
    for branch, files in (("af--set-rc", ["s1", "s2", "s3"]), ("af--b2b-link", ["b1", "b2"]), ("af--genre-keys", ["g1"])):
        _git(tmp_path, "switch", "-q", "-c", branch, "main")
        for name in files:
            _commit(tmp_path, name)
    _git(tmp_path, "switch", "-q", "main")
    return tmp_path


def _stamp(repo: Path, head: str, previous: str | None = None) -> dict:
    return compute_lineage(repo, previous_git_sha_full=previous, head=head, config=CONFIG)


@pytest.mark.requirement("INSTALL-44")
def test_shipped_config_gives_main_white_and_set_rc_cerulean() -> None:
    """[if] the shipped lineage config does not give main white and af--set-rc cerulean [then] the badge lies about the line, [else stop]."""
    config = load_lineage_config(LINEAGE_CONFIG_PATH)
    assert config.color_of("main") == "white"
    assert config.color_of("af--set-rc") == "cerulean"
    assert lineage_color(config, {"af--set-rc": 1.0, "af--b2b-link": 1.0})[0] == "teal"


@pytest.mark.requirement("INSTALL-44")
def test_each_line_wears_its_own_colour(repo: Path) -> None:
    """[if] a build is one line [then] it wears that line's colour, [else stop]."""
    main = _stamp(repo, "main")
    assert (main["color_name"], main["lines"]) == ("white", ["main"])
    assert main["unresolved_lines"] == ["af--gone"]
    set_rc = _stamp(repo, "af--set-rc")
    assert (set_rc["color_name"], set_rc["color_hex"], set_rc["lines"]) == ("cerulean", "#007ba7", ["af--set-rc"])
    assert set_rc["line_shares"] == {"af--set-rc": 1.0}
    assert _stamp(repo, "af--genre-keys")["color_name"] == "white"  # a feature off main is main's line


@pytest.mark.requirement("INSTALL-44")
def test_a_build_carrying_two_lines_is_their_blend_until_they_merge(repo: Path) -> None:
    """[if] set-rc and b2b-link meet in one build [then] it is teal; once merged to main it is white, [else stop]."""
    _git(repo, "switch", "-q", "-c", "af--mix", "af--set-rc")
    _git(repo, "merge", "-q", "--no-edit", "af--b2b-link")
    mixed = _stamp(repo, "af--mix")
    assert mixed["color_name"] == "teal"
    assert mixed["lines"] == ["af--b2b-link", "af--set-rc"]
    _git(repo, "switch", "-q", "-c", "af--between", "af--set-rc~1")
    _git(repo, "merge", "-q", "--no-edit", "af--b2b-link~1")
    between = _stamp(repo, "af--between")
    assert between["line_shares"] == {"af--set-rc": round(2 / 3, 4), "af--b2b-link": 0.5}
    assert between["color_name"] == "teal"
    _git(repo, "switch", "-q", "-c", "af--picked", "main")
    _git(repo, "merge", "-q", "--no-edit", "af--set-rc~2")  # one of set-rc's three
    picked = _stamp(repo, "af--picked")
    assert (picked["color_name"], picked["lines"]) == ("white", ["main"])
    assert picked["line_shares"] == {"af--set-rc": round(1 / 3, 4)}
    _git(repo, "switch", "-q", "main")
    _git(repo, "merge", "-q", "--no-edit", "af--mix")
    assert _stamp(repo, "main")["color_name"] == "white"


@pytest.mark.requirement("INSTALL-44")
def test_a_build_that_drops_the_running_builds_commits_is_not_descended(repo: Path) -> None:
    """[if] genre-keys replaces a set-rc build [then] descent is false, 3 commits, from af--set-rc, [else stop]."""
    running = _git(repo, "rev-parse", "af--set-rc")
    dropped = _stamp(repo, "af--genre-keys", previous=running)
    assert (dropped["descends_from_previous"], dropped["missing_commits"], dropped["missing_from"]) == (False, 3, "af--set-rc")
    assert not_descended_warning(3, "af--set-rc") == "NOT DESCENDED FROM YOUR LAST BUILD: missing 3 commits from af--set-rc"
    _git(repo, "switch", "-q", "af--set-rc")
    newer = _commit(repo, "s4")
    kept = _stamp(repo, newer, previous=running)
    assert (kept["descends_from_previous"], kept["missing_commits"], kept["missing_from"]) == (True, 0, None)
    assert _stamp(repo, "main")["descends_from_previous"] is None


@pytest.mark.requirement("INSTALL-44")
def test_unknowable_lineage_fails_loudly(repo: Path, tmp_path: Path) -> None:
    """[if] lineage cannot be read from git [then] it fails loudly instead of guessing a colour, [else stop]."""
    with pytest.raises(LineageError, match="not in this clone"):
        _stamp(repo, "main", previous="f" * 40)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"trunk": "main", "lines": [{"name": "main", "ref": "x", "color": "mauve"}], "palette": {}}))
    with pytest.raises(LineageError, match="not in the palette"):
        load_lineage_config(bad)
