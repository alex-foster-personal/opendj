"""A format-only commit may change a docstring only the way ruff 0.16.3 re-lays it.

`prove` in `scripts/format_proof.py` compares every changed Python file's AST, and
a docstring is part of it: `__doc__` carries the text, and doctests read its
relative indentation. ruff re-lays docstrings, so `prove` accepts ruff's own
re-layout of each one and nothing looser. The rest of `prove` is tested in
`test_format_proof.py`.

Regression lines:
  - if prove passes a changed docstring relative indentation (a doctest) then broken
  - if prove fails a docstring that was only re-indented as a whole then broken
  - if prove passes a docstring tab made spaces, a form feed made a newline or a blank first line dropped then broken
  - if prove fails ruff's docstring re-layout (a trailing form feed or no-break space, an indent tab) then broken
  - if prove passes ruff re-laying a docstring with an escape or a space before an indent tab then broken
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import format_proof
from tests.quality.format_proof_repo import commit_files, init_repo


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo")


def test_prove_rejects_a_changed_doctest_indentation(repo: Path) -> None:
    before = 'def f():\n    """Doc.\n\n    >>> g()\n        1\n    """\n'
    after = 'def f():\n    """Doc.\n\n    >>> g()\n    1\n    """\n'
    base = commit_files(repo, {"m.py": before}, "init")
    head = commit_files(repo, {"m.py": after}, "style(format): ruff@0.16.3 m")
    assert format_proof.prove(repo, base, head).exit_code == 1


def test_prove_control_a_docstring_reindented_as_a_whole_still_proves(repo: Path) -> None:
    """Opposite-direction control: moving every line by the same amount keeps the relative indentation."""
    before = 'class C:\n    def f(self):\n        """Doc.\n\n    >>> g()\n        1\n    """\n'
    after = 'class C:\n    def f(self):\n        """Doc.\n\n        >>> g()\n            1\n        """\n'
    base = commit_files(repo, {"m.py": before}, "init")
    head = commit_files(repo, {"m.py": after}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 0, result.lines


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ('def f():\n    """a\tb."""\n', 'def f():\n    """a       b."""\n'),
        ('def f():\n    """Doc.\x0cMore."""\n', 'def f():\n    """Doc.\n    More."""\n'),
        ('def f():\n    """Doc.\u2028More."""\n', 'def f():\n    """Doc.\n    More."""\n'),
        ('def f():\n    """\n    Doc.\n    """\n', 'def f():\n    """Doc.\n    """\n'),
        ('def f():\n    """Doc.\\t"""\n', 'def f():\n    """Doc."""\n'),
        ('def f():\n    """Doc.\x1c"""\n', 'def f():\n    """Doc."""\n'),
    ],
    ids=[
        "interior-tab-to-spaces",
        "form-feed-to-newline",
        "line-separator-to-newline",
        "leading-blank-line-dropped",
        "escaped-tab-dropped",
        "trailing-group-separator-dropped",
    ],
)
def test_prove_rejects_docstring_whitespace_ruff_never_changes(repo: Path, before: str, after: str) -> None:
    """`__doc__` carries these, and ruff keeps them: a tab between words, a line broken only at a form feed or
    U+2028, a blank first line, an escaped tab, and U+001C, which Python strips but ruff's whitespace does not."""
    base = commit_files(repo, {"m.py": before}, "init")
    head = commit_files(repo, {"m.py": after}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 1
    assert any("AST differs" in line for line in result.lines), result.lines


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ('def f():\n    """Doc.\x0c\n\n    More.\n    """\n', 'def f():\n    """Doc.\n\n    More.\n    """\n'),
        (
            'def f():\n\t"""Doc.\n\n\tMore.\n\t"""\n\tpass\n',
            'def f():\n    """Doc.\n\n    More.\n    """\n    pass\n',
        ),
        ('def f():\n    """Doc.\n\n    X\n\tY\n    """\n', 'def f():\n    """Doc.\n\n    X\n        Y\n    """\n'),
        ('def f():\n    """Doc.\n\n    X\n\t  Y\n    """\n', 'def f():\n    """Doc.\n\n    X\n          Y\n    """\n'),
        ('def f():\n    """Doc.\xa0\n\n    More.\n    """\n', 'def f():\n    """Doc.\n\n    More.\n    """\n'),
        ('def f():\n    """   Doc."""\n', 'def f():\n    """Doc."""\n'),
        ('def f():\n    """Doc.\u3000"""\n', 'def f():\n    """Doc."""\n'),
    ],
    ids=[
        "trailing-form-feed-stripped",
        "tab-indented-file-reindented",
        "indent-tab-is-8-columns",
        "indent-tab-then-spaces",
        "trailing-no-break-space-stripped",
        "first-line-leading-space-stripped",
        "trailing-ideographic-space-stripped",
    ],
)
def test_prove_control_ruff_docstring_relayout_still_proves(repo: Path, before: str, after: str) -> None:
    """Opposite-direction control, ruff 0.16.3's real output: it strips each line's trailing whitespace and the first
    line's leading whitespace, and re-indents the rest with tabs at 8-column stops, over Unicode's whitespace."""
    base = commit_files(repo, {"m.py": before}, "init")
    head = commit_files(repo, {"m.py": after}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 0, result.lines


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            'def f():\n    """Doc.\\t\n\n            More.\n    """\n',
            'def f():\n    """Doc.\\t\n\n    More.\n    """\n',
        ),
        (
            'def f():\n    """Doc.\n\n    Example:\n    \tcode\n    """\n',
            'def f():\n    """Doc.\n\n    Example:\n        code\n    """\n',
        ),
    ],
    ids=["an-escape", "a-space-before-an-indent-tab"],
)
def test_prove_fails_safe_when_ruff_re_lays_a_docstring_it_cannot_prove(repo: Path, before: str, after: str) -> None:
    """The rule's known limit, pinned on ruff 0.16.3's real output: `__doc__` cannot tell an escaped tab from a
    literal one, and an indent that is not tabs then spaces has no column count ruff agrees with, so either docstring
    is compared exactly and ruff re-laying it fails. The real tree has none of either (review 1l)."""
    base = commit_files(repo, {"m.py": before}, "init")
    head = commit_files(repo, {"m.py": after}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 1
    assert any("AST differs" in line for line in result.lines), result.lines


def test_prove_control_the_normalizer_does_not_hide_a_real_string_edit(repo: Path) -> None:
    """Opposite-direction control: whitespace inside a NON-docstring string is data."""
    base = commit_files(repo, {"m.py": 'def f():\n    x = "a  "\n    return x\n'}, "init")
    head = commit_files(repo, {"m.py": 'def f():\n    x = "a"\n    return x\n'}, "style(format): ruff@0.16.3 m")
    assert format_proof.prove(repo, base, head).exit_code == 1
