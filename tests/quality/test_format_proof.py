"""A format-only commit must be PROVEN format-only, and blame must keep skipping it.

Issue #4456 lands a whole-tree formatting pass in parts. Two things can quietly
go wrong, and each has its own command in `scripts/format_proof.py`:

  - `prove` compares every changed Python file's AST before and after, with
    docstring whitespace normalized the way Black's own safety check does it.
    A "format" commit that also changed a value, renamed a name or added a
    file is a real edit, and blame must not be told to skip it.
  - `ignore-revs` checks `.git-blame-ignore-revs`. A squash or rebase merge
    writes a new SHA onto main, so the listed one names a commit main never
    contains and GitHub silently stops skipping the format commit.

Every test builds its own throwaway git repository, so nothing here depends on
this checkout's history or on whether CI cloned it shallow.

Regression lines:
  - if prove passes a commit that changed a value then broken
  - if prove fails a pure re-wrap or a docstring-whitespace-only change then broken
  - if prove passes a changed docstring's TEXT then broken
  - if prove reports a pass when a file does not parse, or when no Python changed, then broken
  - if prove passes a commit that added, deleted or renamed a file then broken
  - if prove passes a range that also edits a file that is not Python then broken
  - if prove passes a changed `# type:` comment or a changed or removed type-ignore then broken
  - if prove fails a re-wrap that only moves a type-ignore to another line then broken
  - if prove passes a type-ignore or noqa moved to ANOTHER statement then broken
  - if prove passes a comment that was added, removed, reworded or reordered then broken
  - if prove fails a re-wrap that keeps a noqa on its statement, or the ratified `#---` to `# ---` then broken
  - if prove passes a whitespace edit ruff never makes (`# no sec` to `# nosec`, a shebang re-spaced) then broken
  - if prove passes a changed docstring relative indentation (a doctest) then broken
  - if prove fails a docstring that was only re-indented as a whole then broken
  - if prove passes a chmod-only change then broken
  - if ignore-revs passes a SHA that is not an ancestor of HEAD then broken
  - if ignore-revs passes a listed commit whose subject is not style(format): then broken
  - if ignore-revs passes an abbreviated SHA, or a SHA that is not a commit, then broken
  - if ignore-revs reports a pass in a shallow clone that cannot see a listed SHA then broken
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import format_proof

# ----- helpers --------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def _commit(repo: Path, files: dict[str, str], subject: str) -> str:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", subject)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    return path


UNFORMATTED = 'def f(a,b):\n    """Add two.   \n\n       Returns the sum."""\n    return a+b\n'
REFORMATTED = 'def f(a, b):\n    """Add two.\n\n    Returns the sum."""\n    return a + b\n'
IGNORE_BEFORE_REWRAP = "def f(a,\n      b):\n    return a+b\ny = f(1, 2)  # type: ignore[call-arg]\n"
IGNORE_AFTER_REWRAP = "def f(a, b):\n    return a + b\ny = f(1, 2)  # type: ignore[call-arg]\n"


# ----- prove ----------------------------------------------------------------


def test_prove_passes_a_pure_reformat(repo: Path) -> None:
    base = _commit(repo, {"m.py": "x=[1,2 ,3]\n"}, "init")
    head = _commit(repo, {"m.py": "x = [1, 2, 3]\n"}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 0, result.lines
    assert result.files_checked == 1


def test_prove_accepts_docstring_whitespace_only(repo: Path) -> None:
    base = _commit(repo, {"m.py": UNFORMATTED}, "init")
    head = _commit(repo, {"m.py": REFORMATTED}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 0, result.lines
    assert result.docstring_normalized == 1


def test_prove_rejects_a_changed_value(repo: Path) -> None:
    base = _commit(repo, {"m.py": "LIMIT=600\n"}, "init")
    head = _commit(repo, {"m.py": "LIMIT = 601\n"}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 1
    assert any("m.py" in line for line in result.lines)


def test_prove_rejects_changed_docstring_text(repo: Path) -> None:
    base = _commit(repo, {"m.py": '"""Old words."""\n'}, "init")
    head = _commit(repo, {"m.py": '"""New words."""\n'}, "style(format): ruff@0.16.3 m")
    assert format_proof.prove(repo, base, head).exit_code == 1


def test_prove_is_unknown_when_a_file_does_not_parse(repo: Path) -> None:
    base = _commit(repo, {"m.py": "x = 1\n"}, "init")
    head = _commit(repo, {"m.py": "x = (\n"}, "style(format): ruff@0.16.3 m")
    assert format_proof.prove(repo, base, head).exit_code == 2


def test_prove_is_unknown_when_no_python_changed(repo: Path) -> None:
    base = _commit(repo, {"m.py": "x = 1\n", "README.md": "a\n"}, "init")
    head = _commit(repo, {"README.md": "b\n"}, "docs: readme")
    assert format_proof.prove(repo, base, head).exit_code == 2


def test_prove_rejects_an_added_file(repo: Path) -> None:
    base = _commit(repo, {"m.py": "x=1\n"}, "init")
    head = _commit(repo, {"m.py": "x = 1\n", "n.py": "y = 2\n"}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 1
    assert any("n.py" in line for line in result.lines)


def test_prove_is_unknown_when_a_non_python_file_also_changed(repo: Path) -> None:
    """No AST can check a TOML edit, so it must never ride along in a proven format commit."""
    base = _commit(repo, {"m.py": "x=1\n", "pyproject.toml": "a = 1\n"}, "init")
    head = _commit(repo, {"m.py": "x = 1\n", "pyproject.toml": "a = 2\n"}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 2
    assert any("pyproject.toml" in line for line in result.lines)


def test_prove_still_fails_a_python_edit_beside_a_non_python_file(repo: Path) -> None:
    base = _commit(repo, {"m.py": "x=1\n", "pyproject.toml": "a = 1\n"}, "init")
    head = _commit(repo, {"m.py": "x = 2\n", "pyproject.toml": "a = 2\n"}, "style(format): ruff@0.16.3 m")
    assert format_proof.prove(repo, base, head).exit_code == 1


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("x = f()  # type: ignore[attr-defined]\n", "x = f()  # type: ignore[arg-type]\n"),
        ("x = f()  # type: ignore\n", "x = f()\n"),
        ("x = []  # type: list[int]\n", "x = []  # type: list[str]\n"),
    ],
    ids=["changed-ignore-tag", "removed-ignore", "changed-type-comment"],
)
def test_prove_rejects_a_type_comment_edit(repo: Path, before: str, after: str) -> None:
    base = _commit(repo, {"m.py": before}, "init")
    head = _commit(repo, {"m.py": after}, "style(format): ruff@0.16.3 m")
    assert format_proof.prove(repo, base, head).exit_code == 1


def test_prove_control_a_rewrap_may_move_a_type_ignore_to_another_line(repo: Path) -> None:
    """Opposite-direction control: an ignore's LINE is layout, so a re-wrap above it still proves."""
    base = _commit(repo, {"m.py": IGNORE_BEFORE_REWRAP}, "init")
    head = _commit(repo, {"m.py": IGNORE_AFTER_REWRAP}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 0, result.lines


def test_prove_rejects_a_mode_change(repo: Path) -> None:
    """A chmod-only change is status M with identical ASTs; dropping an executable bit is not layout."""
    base = _commit(repo, {"m.py": "x = 1\n"}, "init")
    _git(repo, "update-index", "--chmod=+x", "m.py")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "style(format): chmod")
    result = format_proof.prove(repo, base, _git(repo, "rev-parse", "HEAD"))
    assert result.exit_code == 1
    assert any("mode changed" in line for line in result.lines)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("a = f()  # type: ignore[x]\nb = g()\n", "a = f()\nb = g()  # type: ignore[x]\n"),
        ("import os  # noqa: F401\nimport sys\n", "import os\nimport sys  # noqa: F401\n"),
        ("x = 1\n", "x = 1  # noqa: F841\n"),
        ("if x:  # pragma: no cover\n    y = 1\n", "if x:\n    y = 1\n"),
        ("x = run()  # nosec B602\n", "x = run()  # nosec B603\n"),
        ("x = 1  # old words\n", "x = 1  # new words\n"),
        ("x = 1  # first\n# second\ny = 2\n", "x = 1  # second\n# first\ny = 2\n"),
        ("x = run()  # no sec B602\n", "x = run()  # nosec B602\n"),
        ("#!/usr/bin/env python3\nx = 1\n", "# !/usr/bin/env python3\nx = 1\n"),
        ("#!/usr/bin/env python3\nx = 1\n", "#!/usr/bin/env  python3\nx = 1\n"),
    ],
    ids=[
        "ignore-to-other-statement",
        "noqa-to-other-statement",
        "noqa-added",
        "pragma-removed",
        "nosec-changed",
        "prose-reworded",
        "comments-reordered",
        "directive-inner-space",
        "shebang-broken",
        "shebang-respaced",
    ],
)
def test_prove_rejects_a_comment_edit(repo: Path, before: str, after: str) -> None:
    base = _commit(repo, {"m.py": before}, "init")
    head = _commit(repo, {"m.py": after}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 1
    assert any("comment" in line for line in result.lines), result.lines


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("x = foo(a,\n        b)  # noqa: B008\n", "x = foo(a, b)  # noqa: B008\n"),
        ("#--- section\nx=1\n", "# --- section\nx = 1\n"),
        ("x = 1  #noqa:F841\n", "x = 1  # noqa:F841\n"),
        ("x = 1  # note   \n", "x = 1  # note\n"),
        ("#!/usr/bin/env python3\nx=1\n", "#!/usr/bin/env python3\nx = 1\n"),
    ],
    ids=[
        "noqa-rewrap-same-statement",
        "prose-comment-respaced",
        "directive-respaced",
        "trailing-space",
        "shebang-kept",
    ],
)
def test_prove_control_layout_around_comments_still_proves(repo: Path, before: str, after: str) -> None:
    """Opposite-direction control: a comment's line and spacing are layout; only its statement and words count."""
    base = _commit(repo, {"m.py": before}, "init")
    head = _commit(repo, {"m.py": after}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 0, result.lines


def test_prove_rejects_a_changed_doctest_indentation(repo: Path) -> None:
    before = 'def f():\n    """Doc.\n\n    >>> g()\n        1\n    """\n'
    after = 'def f():\n    """Doc.\n\n    >>> g()\n    1\n    """\n'
    base = _commit(repo, {"m.py": before}, "init")
    head = _commit(repo, {"m.py": after}, "style(format): ruff@0.16.3 m")
    assert format_proof.prove(repo, base, head).exit_code == 1


def test_prove_control_a_docstring_reindented_as_a_whole_still_proves(repo: Path) -> None:
    """Opposite-direction control: moving every line by the same amount keeps the relative indentation."""
    before = 'class C:\n    def f(self):\n        """Doc.\n\n    >>> g()\n        1\n    """\n'
    after = 'class C:\n    def f(self):\n        """Doc.\n\n        >>> g()\n            1\n        """\n'
    base = _commit(repo, {"m.py": before}, "init")
    head = _commit(repo, {"m.py": after}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 0, result.lines


def test_prove_control_the_normalizer_does_not_hide_a_real_string_edit(repo: Path) -> None:
    """Opposite-direction control: whitespace inside a NON-docstring string is data."""
    base = _commit(repo, {"m.py": 'def f():\n    x = "a  "\n    return x\n'}, "init")
    head = _commit(repo, {"m.py": 'def f():\n    x = "a"\n    return x\n'}, "style(format): ruff@0.16.3 m")
    assert format_proof.prove(repo, base, head).exit_code == 1


# ----- ignore-revs ----------------------------------------------------------


def _ignore_file(repo: Path, *shas: str) -> Path:
    path = repo / ".git-blame-ignore-revs"
    body = "# format-only commits (issue #4456)\n" + "".join(f"# part\n{sha}\n" for sha in shas)
    path.write_text(body)
    return path


def test_ignore_revs_accepts_an_ancestor_format_commit(repo: Path) -> None:
    _commit(repo, {"m.py": "x=1\n"}, "init")
    fmt = _commit(repo, {"m.py": "x = 1\n"}, "style(format): ruff@0.16.3 m")
    result = format_proof.check_ignore_revs(repo, _ignore_file(repo, fmt))
    assert result.exit_code == 0, result.lines
    assert result.files_checked == 1


def test_ignore_revs_rejects_a_sha_that_is_not_an_ancestor(repo: Path) -> None:
    _commit(repo, {"m.py": "x=1\n"}, "init")
    _git(repo, "checkout", "-q", "-b", "side")
    side = _commit(repo, {"m.py": "x = 1\n"}, "style(format): ruff@0.16.3 m")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, {"n.py": "y = 1\n"}, "feat: n")
    assert format_proof.check_ignore_revs(repo, _ignore_file(repo, side)).exit_code == 1


def test_ignore_revs_rejects_a_non_format_subject(repo: Path) -> None:
    _commit(repo, {"m.py": "x=1\n"}, "init")
    real = _commit(repo, {"m.py": "x = 2\n"}, "fix: change x")
    assert format_proof.check_ignore_revs(repo, _ignore_file(repo, real)).exit_code == 1


def test_ignore_revs_rejects_an_abbreviated_sha(repo: Path) -> None:
    """git and GitHub want unabbreviated names; a prefix of a REAL format commit must still fail."""
    _commit(repo, {"m.py": "x=1\n"}, "init")
    fmt = _commit(repo, {"m.py": "x = 1\n"}, "style(format): ruff@0.16.3 m")
    path = repo / ".git-blame-ignore-revs"
    path.write_text(f"# header\n{fmt[:12]}\n")
    assert format_proof.check_ignore_revs(repo, path).exit_code == 1


def test_ignore_revs_rejects_a_sha_that_is_not_a_commit(repo: Path) -> None:
    _commit(repo, {"m.py": "x=1\n"}, "init")
    assert format_proof.check_ignore_revs(repo, _ignore_file(repo, "0" * 40)).exit_code == 1


def test_ignore_revs_is_unknown_in_a_shallow_clone(repo: Path, tmp_path: Path) -> None:
    _commit(repo, {"m.py": "x=1\n"}, "init")
    fmt = _commit(repo, {"m.py": "x = 1\n"}, "style(format): ruff@0.16.3 m")
    _commit(repo, {"n.py": "y = 1\n"}, "feat: n")
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{repo}", str(shallow)], check=True)
    assert format_proof.check_ignore_revs(shallow, _ignore_file(shallow, fmt)).exit_code == 2


def test_ignore_revs_accepts_an_empty_list(repo: Path) -> None:
    """The file lands before any part does; a header-only file is valid, and says so."""
    _commit(repo, {"m.py": "x=1\n"}, "init")
    result = format_proof.check_ignore_revs(repo, _ignore_file(repo))
    assert result.exit_code == 0
    assert result.files_checked == 0
