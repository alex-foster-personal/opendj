"""Inside a region ruff 0.16.3 does not format, a format-only commit leaves comments and docstrings as they are.

`prove` in `scripts/format_proof.py` lets a comment change only to what ruff makes of it, and a docstring only to
ruff's re-layout of it. ruff does neither inside a `# fmt: off` (or `# yapf: disable`) region, nor on the other
lines of a statement that ends in a trailing `# fmt: skip`, so there a change to either is a hand edit that blame
must not skip. `scripts/format_proof_regions.py` reads where they are. Every control below is ruff 0.16.3's real
output, measured in review 1q. The rest of `prove` is tested in `test_format_proof.py` and
`test_format_proof_docstrings.py`.

Regression lines:
  - if prove passes a comment normalized inside `# fmt: off`, `# yapf: disable` or a skipped statement then broken
  - if prove passes a docstring re-laid inside `# fmt: off` or on a statement with `# fmt: skip` then broken
  - if prove passes a pragma normalized where ruff does not honor it (nested, or inside a region) then broken
  - if prove ends a region at a `# fmt: on` nested deeper than it, or at one with more text, then broken
  - if prove keeps a region open past a `# fmt: on` between two of its statements, at any column, then broken
  - if prove counts a tab as more than one column when placing a `# fmt: on`, as ruff does not, then broken
  - if prove fails ruff normalizing the pragmas it honors, or anything after a region ends, then broken
  - if prove fails ruff formatting past a pragma it ignores (trailing, in brackets, with text, upper case) then broken
  - if prove fails ruff normalizing a skip comment itself, or a statement after a skipped one, then broken
  - if prove keeps a region whose first statement is simple and skipped, which ruff formats past, then broken
  - if prove lets a later skip, a skipped compound header or a trailing `# fmt: on` end a region then broken
  - if prove passes a region ruff ends early (a dedented comment, an `elif`) then broken: it is not modeled
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import format_proof
from tests.quality.format_proof_repo import commit_files, init_repo


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo")


def _prove(repo: Path, before: str, after: str) -> format_proof.Result:
    base = commit_files(repo, {"m.py": before}, "init")
    head = commit_files(repo, {"m.py": after}, "style(format): ruff@0.16.3 m")
    return format_proof.prove(repo, base, head)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("# fmt: off\nx=[1,2]  #keep\n# fmt: on\n", "# fmt: off\nx=[1,2]  # keep\n# fmt: on\n"),
        ("# fmt: off\n#own\nx=1\n", "# fmt: off\n# own\nx=1\n"),
        ("# fmt: off\nx=[1,2]  # keep   \n", "# fmt: off\nx=[1,2]  # keep\n"),
        ("#\tfmt:\toff\nx=[1,2]  #keep\n", "# \tfmt:\toff\nx=[1,2]  # keep\n"),
        ("# yapf: disable\nx=[1,2]  #keep\n# yapf: enable\n", "# yapf: disable\nx=[1,2]  # keep\n# yapf: enable\n"),
        ('x = f"("\n# fmt: off\ny=[1,2]  #keep\n', 'x = f"("\n# fmt: off\ny=[1,2]  # keep\n'),
        ("# fmt: off\ndef g():\n    return 1  #also\n", "# fmt: off\ndef g():\n    return 1  # also\n"),
        (
            "def f():\n    # fmt: off\n    x=[1,2]  #keep\n    return x\n",
            "def f():\n    # fmt: off\n    x=[1,2]  # keep\n    return x\n",
        ),
        (
            "# fmt: off\nx=[1,2]\ndef g():\n    # fmt: on\n    y=[3,4]  #inner\n",
            "# fmt: off\nx=[1,2]\ndef g():\n    # fmt: on\n    y=[3,4]  # inner\n",
        ),
        (
            "# fmt: off\ndef g():\n    x=[1]\n    # fmt: on\nz=[5,6]  #outer\n",
            "# fmt: off\ndef g():\n    x=[1]\n    # fmt: on\nz=[5,6]  # outer\n",
        ),
        (
            "# fmt: off\nx=[1,2]\n# fmt: on because\ny=[3,4]  #next\n",
            "# fmt: off\nx=[1,2]\n# fmt: on because\ny=[3,4]  # next\n",
        ),
        ("# fmt: off\ndef g():\n    #fmt: on   \n    y=[1]\n", "# fmt: off\ndef g():\n    # fmt: on\n    y=[1]\n"),
        ("# fmt: off\nx=[1]\n#fmt: off   \n", "# fmt: off\nx=[1]\n# fmt: off\n"),
        (
            "# fmt: off\nif a:\n    x=[1]\n    # fmt: on\ny=[2]  #y\n",
            "# fmt: off\nif a:\n    x=[1]\n    # fmt: on\ny=[2]  # y\n",
        ),
        (
            "def f():\n\t# fmt: off\n\tif a:\n\t\tx=[1]\n    # fmt: on\n\ty=[2]  #y\n",
            "def f():\n\t# fmt: off\n\tif a:\n\t\tx=[1]\n    # fmt: on\n\ty=[2]  # y\n",
        ),
        ("x=[  #keep\n    1,2\n]  # fmt: skip\n", "x=[  # keep\n    1,2\n]  # fmt: skip\n"),
        ("x=[  #keep\n    1,2\n]  # fmt: skip  # noqa\n", "x=[  # keep\n    1,2\n]  # fmt: skip  # noqa\n"),
        (
            "def f(\n    a,  #keep\n):  # fmt: skip\n    return a\n",
            "def f(\n    a,  # keep\n):  # fmt: skip\n    return a\n",
        ),
        ("# fmt: off\ny=[1,2]  # fmt: on\nz=[3]  #keep\n", "# fmt: off\ny=[1,2]  # fmt: on\nz=[3]  # keep\n"),
        ("# fmt: off\nx=[1]  #k\ny=[1,2]  #fmt: skip\n", "# fmt: off\nx=[1]  #k\ny=[1,2]  # fmt: skip\n"),
        (
            "# fmt: off\nx=[1]  #k\nif a:\n    y=[1,2]  #fmt: skip\n    z=[3]  #keep\n",
            "# fmt: off\nx=[1]  #k\nif a:\n    y=[1,2]  #fmt: skip\n    z=[3]  # keep\n",
        ),
        ("# fmt: off\nif a:  #fmt: skip\n    z=[3]  #keep\n", "# fmt: off\nif a:  #fmt: skip\n    z=[3]  # keep\n"),
        ("# fmt: off\nif a: x=[1]  #fmt: skip\nz=[3]  #keep\n", "# fmt: off\nif a: x=[1]  #fmt: skip\nz=[3]  # keep\n"),
        (
            "# fmt: off\n@dec(1,2)  #fmt: skip\ndef f():  #c\n    pass\n",
            "# fmt: off\n@dec(1,2)  #fmt: skip\ndef f():  # c\n    pass\n",
        ),
        (
            "# fmt: off\ny=[1,2]  # fmt: skip please\nz=[3]  #keep\n",
            "# fmt: off\ny=[1,2]  # fmt: skip please\nz=[3]  # keep\n",
        ),
        (
            "# fmt: off\nif a:\n    x=[1]  #k\n# fmt: skip\ny=[2]  #keep\n",
            "# fmt: off\nif a:\n    x=[1]  #k\n# fmt: skip\ny=[2]  # keep\n",
        ),
    ],
    ids=[
        "off-region-trailing-comment-spaced",
        "off-region-own-line-comment-spaced",
        "off-region-trailing-whitespace-stripped",
        "tab-spelled-off-region",
        "yapf-region",
        "off-after-an-f-string-holding-a-bracket",
        "off-region-running-to-the-end-of-the-file",
        "off-region-inside-a-block",
        "on-nested-deeper-does-not-end-the-region",
        "on-in-an-inner-block-does-not-end-the-region",
        "on-with-more-text-does-not-end-the-region",
        "unhonored-on-normalized",
        "second-off-inside-a-region-normalized",
        "on-at-a-nested-block-column-does-not-end-the-region",
        "space-indented-on-in-a-nested-tab-block-does-not-end-the-region",
        "skipped-statement-comment-spaced",
        "skipped-statement-with-noqa-after-the-skip",
        "skipped-compound-header-comment-spaced",
        "trailing-on-does-not-end-the-region",
        "skip-after-the-first-statement-of-a-region-kept",
        "skip-in-a-nested-block-of-a-region-kept",
        "skipped-compound-header-leading-a-region-keeps-it",
        "one-line-compound-leading-a-region-keeps-it",
        "skipped-decorator-leading-a-region-keeps-it",
        "skip-with-more-text-leading-a-region-keeps-it",
        "own-line-skip-in-a-compound-led-region-keeps-it",
    ],
)
def test_prove_rejects_a_comment_normalized_where_ruff_does_not_format(repo: Path, before: str, after: str) -> None:
    """Each head is what ruff makes of the comment ELSEWHERE, so only the region tells it from a hand edit."""
    result = _prove(repo, before, after)
    assert result.exit_code == 1
    assert any("a comment was" in line for line in result.lines), result.lines


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            '# fmt: off\ndef f():\n    """Doc.   """\n# fmt: on\n',
            '# fmt: off\ndef f():\n    """Doc."""\n# fmt: on\n',
        ),
        (
            'def f():\n    # fmt: off\n    def g():\n        """Doc.   """\n',
            'def f():\n    # fmt: off\n    def g():\n        """Doc."""\n',
        ),
        ('def f():\n    """Doc.   """  # fmt: skip\n', 'def f():\n    """Doc."""  # fmt: skip\n'),
        (
            'def f():\n    """Doc.\n\n        More.\n    """  # fmt: skip\n',
            'def f():\n    """Doc.\n\n    More.\n    """  # fmt: skip\n',
        ),
    ],
    ids=["off-region", "off-region-inside-a-block", "skipped-docstring", "skipped-multi-line-docstring"],
)
def test_prove_rejects_a_docstring_re_laid_where_ruff_does_not_format(repo: Path, before: str, after: str) -> None:
    result = _prove(repo, before, after)
    assert result.exit_code == 1
    assert any("AST differs" in line for line in result.lines), result.lines


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            "# fmt: off\nx=[1,2]  #keep\n#own\n# fmt: on\ny=1  #fix\n",
            "# fmt: off\nx=[1,2]  #keep\n#own\n# fmt: on\ny = 1  # fix\n",
        ),
        ("#fmt: off\nx=[1,2]  #keep\n#fmt: on\ny=1  #fix\n", "# fmt: off\nx=[1,2]  #keep\n# fmt: on\ny = 1  # fix\n"),
        (
            "# fmt: off   \nx=[1,2]  #keep   \n# fmt: on   \ny=1  #fix   \n",
            "# fmt: off\nx=[1,2]  #keep   \n# fmt: on\ny = 1  # fix\n",
        ),
        ("# fmt:off\nx=[1,2]  #keep\n# fmt:on\ny=1  #fix\n", "# fmt:off\nx=[1,2]  #keep\n# fmt:on\ny = 1  # fix\n"),
        (
            "# yapf: disable\nx=[1,2]  #keep\n# yapf: enable\ny=1  #fix\n",
            "# yapf: disable\nx=[1,2]  #keep\n# yapf: enable\ny = 1  # fix\n",
        ),
        ("#\tfmt:\toff\nx=[1,2]  #keep\n", "# \tfmt:\toff\nx=[1,2]  #keep\n"),
        ("# fmt: off\nx=[1]  #k\n#fmt: on   \n", "# fmt: off\nx=[1]  #k\n# fmt: on\n"),
        (
            "def f():\n    # fmt: off\n    x=[1,2]  #keep\n    return x\ny=1  #after\n",
            "def f():\n    # fmt: off\n    x=[1,2]  #keep\n    return x\n\n\ny = 1  # after\n",
        ),
        (
            "def f():\n    # fmt: off\n    x=[1,2]  #keep\n#low\n    return x\ny=1  #after\n",
            "def f():\n    # fmt: off\n    x=[1,2]  #keep\n    #low\n    return x\n\n\ny = 1  # after\n",
        ),
        (
            "def f():\n# fmt: off\n    x=[1,2]  #keep\n    return x\ny=1  #after\n",
            "def f():\n    # fmt: off\n    x=[1,2]  #keep\n    return x\n\n\ny = 1  # after\n",
        ),
        (
            "def f():\n    # fmt: off\n    x=[1,2]  #keep\n# fmt: on\n    y=[3,4]  #still\n",
            "def f():\n    # fmt: off\n    x=[1,2]  #keep\n    # fmt: on\n    y = [3, 4]  # still\n",
        ),
        (
            "class A:\n    # fmt: off\n    x=[1]  #k\n    #fmt: on   \ny=1  #y\n",
            "class A:\n    # fmt: off\n    x=[1]  #k\n    # fmt: on\n\n\ny = 1  # y\n",
        ),
        (
            "class A:\n    # fmt: off\n    x=[1]  #k\n# fmt: on\ny=[2]  #y\n",
            "class A:\n    # fmt: off\n    x=[1]  #k\n\n\n# fmt: on\ny = [2]  # y\n",
        ),
        (
            "class A:\n    # fmt: off\n    x=[1,2]  #keep\n    def m(self):  #m\n        return 1  #r\n"
            "    # fmt: on\n    y=[3,4]  #y\n",
            "class A:\n    # fmt: off\n    x=[1,2]  #keep\n    def m(self):  #m\n        return 1  #r\n"
            "    # fmt: on\n    y = [3, 4]  # y\n",
        ),
        (
            "# fmt: off\n@dec  #d\ndef f():  #c\n    pass  #p\n# fmt: on\ny=[1]  #y\n",
            "# fmt: off\n@dec  #d\ndef f():  #c\n    pass  #p\n# fmt: on\ny = [1]  # y\n",
        ),
        (
            "@dec\n# fmt: off\n@dec2\n# fmt: on\ndef f():  #c\n    pass\n",
            "@dec\n# fmt: off\n@dec2\n# fmt: on\ndef f():  # c\n    pass\n",
        ),
        (
            "if a:\n    # fmt: off\n    x=[1]  #k\nelse:\n    y=[1]  #y\n",
            "if a:\n    # fmt: off\n    x=[1]  #k\nelse:\n    y = [1]  # y\n",
        ),
        ("# fmt: off\nx=[1]  #k\n    # fmt: on\ny=[2]  #y\n", "# fmt: off\nx=[1]  #k\n    # fmt: on\ny = [2]  # y\n"),
        (
            "def f():\n    # fmt: off\n    x=[1]  #k\n\t# fmt: on\n    y=[2]  #y\n",
            "def f():\n    # fmt: off\n    x=[1]  #k\n    # fmt: on\n    y = [2]  # y\n",
        ),
        (
            "def f():\n    # fmt: off\n    if a:\n        x=[1]  #k\n      # fmt: on\n    y=[2]  #y\n",
            "def f():\n    # fmt: off\n    if a:\n        x=[1]  #k\n      # fmt: on\n    y = [2]  # y\n",
        ),
        (
            "def f():\n    # fmt: off\n    if a:\n        x=[1]  #k\n\t# fmt: on\n    y=[2]  #y\n",
            "def f():\n    # fmt: off\n    if a:\n        x=[1]  #k\n    # fmt: on\n    y = [2]  # y\n",
        ),
        (
            '# fmt: off\ndef f():\n    """Doc.   """\n# fmt: on\ndef g():\n    """Doc.   """\n',
            '# fmt: off\ndef f():\n    """Doc.   """\n# fmt: on\ndef g():\n    """Doc."""\n',
        ),
        (
            "def f():\n    # fmt: off\n    if a:\n        x=[1]  #k\n    z=[2]  #z\n        # fmt: on\n    y=[3]  #y\n",
            "def f():\n    # fmt: off\n    if a:\n        x=[1]  #k\n    z=[2]  #z\n        # fmt: on\n    y = [3]  # y\n",
        ),
        (
            "# fmt: off\nx=[1]  #k\n#fmt: on\n#fmt: on\ny=[2]  #y\n",
            "# fmt: off\nx=[1]  #k\n# fmt: on\n# fmt: on\ny = [2]  # y\n",
        ),
    ],
    ids=[
        "region-kept-and-formatting-resumed-after-on",
        "unspaced-pragmas-normalized",
        "pragma-trailing-whitespace-stripped",
        "unspaced-colon-pragmas",
        "yapf-pragmas",
        "tab-spelled-off-normalized",
        "on-at-the-end-of-the-file-normalized",
        "region-ended-by-a-dedent",
        "dedented-comment-inside-the-region-re-indented",
        "off-below-its-block-re-indented",
        "on-at-a-lower-column-ends-the-region",
        "on-closing-a-block-normalized",
        "on-after-the-block-it-closes",
        "region-spanning-a-method",
        "region-spanning-a-decorated-def",
        "region-between-decorators",
        "region-ended-by-else",
        "on-at-any-column-between-two-statements-of-the-block",
        "tab-indented-on-in-a-space-indented-block",
        "on-dedented-out-of-a-nested-block",
        "tab-indented-on-dedented-out-of-a-nested-space-block",
        "docstring-re-laid-after-on",
        "on-after-a-nested-block-closed-by-a-level-statement",
        "two-on-pragmas-in-a-row-normalized",
    ],
)
def test_prove_control_ruff_output_around_a_formatter_disabled_region_still_proves(
    repo: Path, before: str, after: str
) -> None:
    """Opposite-direction control: ruff normalizes the pragmas it honors, and formats everything the region ends
    before, so the region must not reach further than ruff's does."""
    result = _prove(repo, before, after)
    assert result.exit_code == 0, result.lines


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            "# fmt: off\ny=[1,2]  #fmt: skip\nz=[3]  #keep\nv=[5]  #v\n",
            "# fmt: off\ny=[1,2]  # fmt: skip\nz = [3]  # keep\nv = [5]  # v\n",
        ),
        (
            "def f():\n    # fmt: off\n    y=[1,2]  #fmt: skip\n    z=[3]  #keep\n",
            "def f():\n    # fmt: off\n    y=[1,2]  # fmt: skip\n    z = [3]  # keep\n",
        ),
        (
            "class A:\n    # fmt: off\n    y=[1,2]  #fmt: skip\n    z=[3]  #keep\nw=[4]  #w\n",
            "class A:\n    # fmt: off\n    y=[1,2]  # fmt: skip\n    z = [3]  # keep\n\n\nw = [4]  # w\n",
        ),
        (
            "# fmt: off\ny=[  #in\n1,2]  #fmt: skip\nz=[3]  #keep\n",
            "# fmt: off\ny=[  #in\n1,2]  # fmt: skip\nz = [3]  # keep\n",
        ),
        (
            "# fmt: off\ny=[1,2]  #a  #fmt: skip\nz=[3]  #keep\n",
            "# fmt: off\ny=[1,2]  # a  #fmt: skip\nz = [3]  # keep\n",
        ),
        (
            "# fmt: off\ny=[1,2]  #noqa  # fmt: skip\nz=[3]  #keep\n",
            "# fmt: off\ny=[1,2]  # noqa  # fmt: skip\nz = [3]  # keep\n",
        ),
        (
            "# fmt: off\n#c\ny=[1,2]  #fmt: skip\nz=[3]  #keep\n",
            "# fmt: off\n# c\ny=[1,2]  # fmt: skip\nz = [3]  # keep\n",
        ),
        (
            "# fmt: off\ny=[1,2]  #fmt: skip\n#c\nz=[3]  #keep\n",
            "# fmt: off\ny=[1,2]  # fmt: skip\n# c\nz = [3]  # keep\n",
        ),
        ("# fmt: off\n\ny=[1,2]  #fmt: skip\nz=[3]  #keep\n", "# fmt: off\n\ny=[1,2]  # fmt: skip\nz = [3]  # keep\n"),
        (
            "# fmt: off\nx=1; y=[1,2]  #fmt: skip\nz=[3]  #keep\n",
            "# fmt: off\nx=1; y=[1,2]  # fmt: skip\nz = [3]  # keep\n",
        ),
        (
            "# fmt: off\nx = \\\n  [1]  #fmt: skip\nz=[3]  #keep\n",
            "# fmt: off\nx = \\\n  [1]  # fmt: skip\nz = [3]  # keep\n",
        ),
        (
            "# fmt: off\n'''Doc.'''  #fmt: skip\nz=[3]  #keep\n",
            "# fmt: off\n'''Doc.'''  # fmt: skip\n\nz = [3]  # keep\n",
        ),
        (
            "# fmt: off\ny=[1,2]  #fmt: skip\nz=[3]  #keep\n#fmt: on\nw=[4]  #w\n",
            "# fmt: off\ny=[1,2]  # fmt: skip\nz = [3]  # keep\n# fmt: on\nw = [4]  # w\n",
        ),
        (
            "# fmt: off\ny=[1,2]  #fmt: skip\n# fmt: off\nz=[3]  #keep\n",
            "# fmt: off\ny=[1,2]  # fmt: skip\n# fmt: off\nz=[3]  #keep\n",
        ),
        (
            "# yapf: disable\ny=[1,2]  #fmt: skip\nz=[3]  #keep\n",
            "# yapf: disable\ny=[1,2]  # fmt: skip\nz = [3]  # keep\n",
        ),
        ("# fmt: off\ny=[1,2]  #fmt: skip\n", "# fmt: off\ny=[1,2]  # fmt: skip\n"),
    ],
    ids=[
        "statements-after-it-formatted",
        "inside-a-def",
        "inside-a-class-body",
        "multi-line-statement-held-whole",
        "comment-before-the-skip-normalized",
        "noqa-before-the-skip-normalized",
        "comment-between-the-off-and-it-normalized",
        "comment-after-it-normalized",
        "blank-line-between-the-off-and-it",
        "semicolon-line-held-whole",
        "backslash-continuation-held-whole",
        "string-statement-held-and-spaced",
        "on-after-it-normalized",
        "off-after-it-opens-a-region",
        "yapf-region",
        "at-the-end-of-the-file",
    ],
)
def test_prove_control_a_region_whose_first_statement_is_skipped_is_no_region(
    repo: Path, before: str, after: str
) -> None:
    """Opposite-direction control: ruff 0.16.3 holds a simple statement that opens a region with a trailing
    `# fmt: skip` alone, formats every comment before and after it, and never opens the region. Each head is its
    real output, measured in review 1q."""
    result = _prove(repo, before, after)
    assert result.exit_code == 0, result.lines


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("x=[1,2]  # fmt: off\ny=[3,4]  #next\n# fmt: on\n", "x=[1,2]  # fmt: off\ny = [3, 4]  # next\n# fmt: on\n"),
        (
            "x = [\n    # fmt: off\n    1,2,  #keep\n    # fmt: on\n]\ny=1  #fix\n",
            "x = [\n    # fmt: off\n    1,\n    2,  # keep\n    # fmt: on\n]\ny = 1  # fix\n",
        ),
        (
            "# fmt: off  # reason\nx=[1,2]  #keep\n# fmt: on\ny=1  #fix\n",
            "# fmt: off  # reason\nx = [1, 2]  # keep\n# fmt: on\ny = 1  # fix\n",
        ),
        ("# do not use fmt: off here\nx=[1,2]  #next\n", "# do not use fmt: off here\nx = [1, 2]  # next\n"),
        ("# FMT: OFF\nx=[1,2]  #keep\n# FMT: ON\n", "# FMT: OFF\nx = [1, 2]  # keep\n# FMT: ON\n"),
        ("# fmt: off\xa0\nx=[1,2]  #keep\n", "# fmt: off\nx = [1, 2]  # keep\n"),
        ("# yapf: disable  # x\nx=[1,2]  #k\n", "# yapf: disable  # x\nx = [1, 2]  # k\n"),
        ("x=[1,2]  #a  # fmt: skip\ny=1  #fix\n", "x=[1,2]  # a  # fmt: skip\ny = 1  # fix\n"),
        ("x=[1,2]  #fmt: skip\n", "x=[1,2]  # fmt: skip\n"),
        ("x=[1,2]  #a  # noqa  # fmt: skip\ny=1  #fix\n", "x=[1,2]  # a  # noqa  # fmt: skip\ny = 1  # fix\n"),
        ("x=[  #keep\n    1,2\n]  #\tfmt: skip\n", "x=[  #keep\n    1,2\n]  # \tfmt: skip\n"),
        (
            "def f(a,b):  #h  # fmt: skip\n    return a+b  #body\n",
            "def f(a,b):  # h  # fmt: skip\n    return a + b  # body\n",
        ),
        (
            "@dec(1,2)  #d  # fmt: skip\ndef f():  #c\n    pass\n",
            "@dec(1,2)  # d  # fmt: skip\ndef f():  # c\n    pass\n",
        ),
        ("# fmt: skip\nx=[1,2]  #a\n", "# fmt: skip\nx = [1, 2]  # a\n"),
        (
            "x = [\n    1,2,  #a  # fmt: skip\n    3,\n]  #b\n",
            "x = [\n    1,\n    2,  # a  # fmt: skip\n    3,\n]  # b\n",
        ),
        ("x=[  #keep\n    1,2\n]  # see fmt: skip docs\n", "x = [  # keep\n    1,\n    2,\n]  # see fmt: skip docs\n"),
        ('def f():  # fmt: skip\n    """Doc.   """\n', 'def f():  # fmt: skip\n    """Doc."""\n'),
        ('# fmt: off\xa0\ndef f():\n    """Doc.   """\n', '# fmt: off\ndef f():\n    """Doc."""\n'),
        (
            "x = [  #o\n    1,2,  #a  # fmt: skip\n    3,\n]  #b\n",
            "x = [  # o\n    1,\n    2,  # a  # fmt: skip\n    3,\n]  # b\n",
        ),
    ],
    ids=[
        "trailing-off-not-honored",
        "off-inside-brackets-not-honored",
        "off-with-more-text-not-honored",
        "off-mentioned-in-prose-not-honored",
        "upper-case-off-not-honored",
        "off-ending-in-a-no-break-space-not-honored",
        "yapf-disable-with-more-text-not-honored",
        "skip-comment-itself-normalized",
        "unspaced-skip-comment-normalized",
        "skip-after-a-noqa",
        "tab-spelled-skip-normalized",
        "skipped-header-leaves-the-body-formatted",
        "skipped-decorator-leaves-the-def-formatted",
        "own-line-skip-not-honored",
        "skip-inside-brackets-not-honored",
        "skip-mentioned-in-prose-not-honored",
        "docstring-below-a-skipped-header-re-laid",
        "docstring-below-an-off-ending-in-a-no-break-space-re-laid",
        "skip-inside-brackets-with-a-comment-above-it",
    ],
)
def test_prove_control_ruff_formatting_past_a_pragma_it_does_not_honor_still_proves(
    repo: Path, before: str, after: str
) -> None:
    """Opposite-direction control: only an exact pragma where ruff reads one disables formatting, so the proof
    must not widen a region to a pragma ruff ignores, or a skip to more than its own statement."""
    result = _prove(repo, before, after)
    assert result.exit_code == 0, result.lines


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            "def f():\n    # fmt: off\n    x=[1,2]\n#between\ny=1  #after\n",
            "def f():\n    # fmt: off\n    x=[1,2]\n\n\n# between\ny = 1  # after\n",
        ),
        (
            "def f():\n    x=1\n    # fmt: off\ny=[1,2]  #after\n",
            "def f():\n    x = 1\n    # fmt: off\n\n\ny = [1, 2]  # after\n",
        ),
        (
            "if a:\n    pass\n# fmt: off\nelif b:\n    x=[1]  #k\n# fmt: on\ny=[1]  #y\n",
            "if a:\n    pass\n# fmt: off\nelif b:\n    x = [1]  # k\n# fmt: on\ny = [1]  # y\n",
        ),
        (
            "# fmt: off\nmatch = [1]  #fmt: skip\nz=[3]  #keep\n",
            "# fmt: off\nmatch = [1]  # fmt: skip\nz = [3]  # keep\n",
        ),
    ],
    ids=["dedented-comment-after-the-region", "off-closing-a-block", "off-before-an-elif", "skipped-soft-keyword-name"],
)
def test_prove_fails_safe_where_ruff_ends_a_region_sooner_than_the_model(repo: Path, before: str, after: str) -> None:
    """The model's known limit, pinned on ruff 0.16.3's real output: ruff gives a dedented comment to the statement
    after the region, and reads an off pragma closing a block or leading an `elif` as no region at all, and neither
    is a region led by a skipped statement whose first name is `match` or `case`, soft keywords the model cannot tell
    from a compound header's. The model keeps each region open, so each fails, the safe side, and the real tree has
    no `# fmt: off` (review 1q)."""
    result = _prove(repo, before, after)
    assert result.exit_code == 1
