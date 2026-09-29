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
  - if prove passes a comment moved past a sibling, across a decorator, or into a module's first statement then broken
  - if prove passes a header pragma moved into the body, or any end-of-line comment made own-line, then broken
  - if prove passes a comment moved past a name inside one statement (a per-argument type comment) then broken
  - if prove passes a comment moved between string args or `...` items, into a bracket or out of a call then broken
  - if prove passes a comment moved across an operator or keyword in one expression (`+`, `==`, `.`, `and`) then broken
  - if prove passes a comment moved into a parenthesized tuple, whose `(` is not counted, then broken
  - if prove passes ruff moving a trailing operator past an end-of-line comment then broken: it is not provable
  - if prove fails ruff joining strings, dropping parentheses or adding commas around a comment then broken
  - if prove fails ruff adding a trailing comma between an item and its comment then broken
  - if prove fails ruff unparenthesizing a commented value (`x = (\n    1  # c\n)` to `x = 1  # c`) then broken
  - if prove fails a semicolon split that keeps the trailing comment on the last statement then broken
  - if prove passes a shebang moved off or onto byte 0 then broken
  - if prove passes a cookie moved out of reach that changes what the bytes decode to then broken
  - if prove fails a cookie moved into reach of an ASCII file, a dropped BOM, or a shebang's trailing space then broken
  - if prove crashes on or fails a latin-1 file re-wrapped under its own cookie then broken
  - if prove passes a changed docstring relative indentation (a doctest) then broken
  - if prove fails a docstring that was only re-indented as a whole then broken
  - if prove passes a chmod-only change then broken
  - if ignore-revs passes a SHA that is not an ancestor of HEAD then broken
  - if ignore-revs passes a listed commit whose subject is not style(format): then broken
  - if ignore-revs passes an abbreviated SHA, a SHA that is not a commit, or an annotated tag's SHA, then broken
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


def _commit(repo: Path, files: dict[str, str | bytes], subject: str) -> str:
    for rel, content in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            (repo / rel).write_bytes(content)
        elif isinstance(content, str):
            (repo / rel).write_text(content)
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
        ('x = ("a"\n     "b")  # c\n', 'x = "ab"  # c\n'),
        ("if (a):  # c\n    pass\n", "if a:  # c\n    pass\n"),
        ("def f(a,  # type: int\n      b):\n    pass\n", "def f(\n    a,  # type: int\n    b,\n):\n    pass\n"),
        ("x: float = (\n    1.36  # c\n)\n", "x: float = 1.36  # c\n"),
        ("def f():\n    return (\n        1  # c\n    )\n", "def f():\n    return 1  # c\n"),
        (
            "try:\n    pass\nexcept E:\n    x = (\n        1  # c\n    )\n",
            "try:\n    pass\nexcept E:\n    x = 1  # c\n",
        ),
        ("x = [\n    1,\n    2  # c\n]\n", "x = [\n    1,\n    2,  # c\n]\n"),
    ],
    ids=[
        "noqa-rewrap-same-statement",
        "prose-comment-respaced",
        "directive-respaced",
        "trailing-space",
        "shebang-kept",
        "implicit-strings-joined",
        "parentheses-dropped",
        "per-argument-comment-expanded",
        "commented-value-unparenthesized",
        "commented-return-unparenthesized",
        "commented-value-in-a-handler-unparenthesized",
        "trailing-comma-added-before-a-comment",
    ],
)
def test_prove_control_layout_around_comments_still_proves(repo: Path, before: str, after: str) -> None:
    """Opposite-direction control: a comment's line and spacing are layout; only its statement and words count."""
    base = _commit(repo, {"m.py": before}, "init")
    head = _commit(repo, {"m.py": after}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 0, result.lines


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            "def f():\n    a = 1\n    # note\n    b = 2\n    c = 3\n",
            "def f():\n    a = 1\n    b = 2\n    # note\n    c = 3\n",
        ),
        ("# type: ignore\nx = (\n    1\n)\n", "x = (  # type: ignore\n    1\n)\n"),
        ("x = foo(\n    a,\n    # c\n)\ny = 1\n", "x = foo(\n    a,\n)\n# c\ny = 1\n"),
        ("@dec\n# c\ndef f():\n    pass\n", "# c\n@dec\ndef f():\n    pass\n"),
        ("if cond:  # pragma: no cover\n    y = 1\n", "if cond:\n    # pragma: no cover\n    y = 1\n"),
        ("x = [  # c\n    1,\n]\n", "x = [\n    # c\n    1,\n]\n"),
        ("x = foo(a,  # type: int\n        b)\n", "x = foo(a,\n        b)  # type: int\n"),
        ('# type: ignore\n"""Doc."""\nx = 1\n', '"""Doc."""\n# type: ignore\nx = 1\n'),
        ('foo(\n    "long",  # noqa: E501\n    "short",\n)\n', 'foo(\n    "long",\n    "short",  # noqa: E501\n)\n'),
        ("x = [\n    ...,  # c\n    ...,\n]\n", "x = [\n    ...,\n    ...,  # c\n]\n"),
        ("x = (\n    # c\n    [\n        1,\n    ]\n)\n", "x = (\n    [\n        # c\n        1,\n    ]\n)\n"),
        ("x = [foo(\n    a,  # c\n), b]\n", "x = [foo(\n    a,\n),  # c\n b]\n"),
        ("if x:\n    pass  # c\nelse:\n    pass\n", "if x:\n    pass\nelse:  # c\n    pass\n"),
        ("x = (\n    a\n    # c\n    + b\n)\n", "x = (\n    a +\n    # c\n    b\n)\n"),
        ("x = (\n    a  # c\n    == b\n)\n", "x = (\n    a ==  # c\n    b\n)\n"),
        ("x = (\n    a\n    # c\n    .b\n)\n", "x = (\n    a.\n    # c\n    b\n)\n"),
        ("x = (\n    a\n    # c\n    and b\n)\n", "x = (\n    a and\n    # c\n    b\n)\n"),
        ("x = [\n    # c\n    (\n        1,\n    ),\n]\n", "x = [\n    (\n        # c\n        1,\n    ),\n]\n"),
    ],
    ids=[
        "past-a-sibling-statement",
        "module-ignore-into-first-statement",
        "out-of-its-statement",
        "across-a-decorator",
        "header-pragma-into-the-body",
        "end-of-line-to-own-line",
        "past-a-name-inside-its-statement",
        "module-ignore-below-the-docstring",
        "between-string-arguments",
        "between-ellipses",
        "into-a-bracket",
        "out-of-a-nested-call",
        "onto-an-else",
        "own-line-across-an-operator",
        "end-of-line-across-a-comparison",
        "across-an-attribute-dot",
        "across-a-keyword-operator",
        "into-a-parenthesized-tuple",
    ],
)
def test_prove_rejects_a_comment_moved_to_another_place_in_the_tree(repo: Path, before: str, after: str) -> None:
    """A comment keeps how many AST nodes open and close before it, how many of the tokens ruff never adds or drops
    precede it (names, keywords, numbers, operators: `else:` and `+` have no node of their own), and whether it ends
    a line of code. Some cases cross exactly one of those: a node start (into-a-parenthesized-tuple, whose `(` ruff
    may drop), a node end (out-of-a-nested-call), a keyword (across-a-keyword-operator), an operator
    (own-line-across-an-operator), a line end (header-pragma-into-the-body). A whole-module type-ignore only works
    above the first statement, and coverage reads `if cond:  # pragma: no cover` as the whole block, a comment-only
    line as nothing."""
    base = _commit(repo, {"m.py": before}, "init")
    head = _commit(repo, {"m.py": after}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 1
    assert any("comment" in line for line in result.lines), result.lines


def test_prove_fails_safe_when_ruff_moves_a_trailing_operator_past_a_comment(repo: Path) -> None:
    """The rule's known limit, pinned so it is not loosened by accident: ruff turns `a +  # c` then `b` into `a  # c`
    then `+ b`, which moves the operator past the comment. No token count tells that from a comment moved across the
    operator, so the proof fails it, and such a comment is re-anchored in its own commit before the part."""
    base = _commit(repo, {"m.py": "x = (a +  # c\n     b)\n"}, "init")
    head = _commit(repo, {"m.py": "x = (\n    a  # c\n    + b\n)\n"}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 1
    assert any("comment" in line for line in result.lines), result.lines


def test_prove_control_a_semicolon_split_keeps_the_trailing_comment(repo: Path) -> None:
    """Opposite-direction control: `x = 1; y = 2  # c` annotates y, and ruff's split leaves it there."""
    base = _commit(repo, {"m.py": "x = 1; y = 2  # c\n"}, "init")
    head = _commit(repo, {"m.py": "x = 1\ny = 2  # c\n"}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 0, result.lines


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (b"#!/usr/bin/env python3\nx = 1\n", b"\n#!/usr/bin/env python3\nx = 1\n"),
        (b"\n#!/usr/bin/env python3\nx=1\n", b"#!/usr/bin/env python3\nx = 1\n"),
        (b"#!/usr/bin/env python3\nx = 1\n", b"# !/usr/bin/env python3\nx = 1\n"),
    ],
    ids=["shebang-off-byte-0", "shebang-onto-byte-0", "shebang-broken"],
)
def test_prove_rejects_a_shebang_that_stops_or_starts_running(repo: Path, before: bytes, after: bytes) -> None:
    """The kernel reads a shebang only at byte 0, so a move that keeps its words and statement still changes how
    the file runs. ruff 0.16.3 makes the second move itself, by dropping the blank lines above it."""
    base = _commit(repo, {"m.py": before}, "init")
    head = _commit(repo, {"m.py": after}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 1
    assert any("shebang" in line for line in result.lines), result.lines


def test_prove_decodes_each_side_by_its_own_cookie(repo: Path) -> None:
    """Python honors a coding cookie only on line 1 or 2. Moved out of reach, the same bytes decode to another
    string, which only a per-side decode can see: a utf-8 read of both sides finds them equal."""
    before = b'# -*- coding: latin-1 -*-\nx = "\xc3\xa9"\n'
    after = b'\n\n# -*- coding: latin-1 -*-\nx = "\xc3\xa9"\n'
    base = _commit(repo, {"m.py": before}, "init")
    head = _commit(repo, {"m.py": after}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 1
    assert any("AST differs" in line for line in result.lines), result.lines


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (b"\n\n# -*- coding: latin-1 -*-\nx=1\n", b"# -*- coding: latin-1 -*-\nx = 1\n"),
        (b"\xef\xbb\xbfx=1\n", b"x = 1\n"),
        (b"#!/usr/bin/env python3 \nx=1\n", b"#!/usr/bin/env python3\nx = 1\n"),
        (b'# -*- coding: latin-1 -*-\nx="\xe9"\n', b'# -*- coding: latin-1 -*-\nx = "\xe9"\n'),
    ],
    ids=["ascii-file-cookie-reaches-line-1", "bom-dropped", "shebang-trailing-space", "latin-1-file-rewrapped"],
)
def test_prove_control_a_position_change_that_changes_nothing_still_proves(
    repo: Path, before: bytes, after: bytes
) -> None:
    """Opposite-direction control, from ruff 0.16.3's real output: what counts is the program each side DECODES
    to and the shebang's words, not the cookie's line, the encoding's name or a BOM Python strips anyway."""
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


def test_ignore_revs_rejects_an_annotated_tag_on_a_format_commit(repo: Path) -> None:
    """git peels `<sha>^{commit}`, so a tag object pointing at a real format commit would pass that probe."""
    _commit(repo, {"m.py": "x=1\n"}, "init")
    fmt = _commit(repo, {"m.py": "x = 1\n"}, "style(format): ruff@0.16.3 m")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "tag", "-a", "-m", "part", "part-1", fmt)
    tag_object = _git(repo, "rev-parse", "part-1")
    assert tag_object != fmt
    result = format_proof.check_ignore_revs(repo, _ignore_file(repo, tag_object))
    assert result.exit_code == 1
    assert any("tag" in line for line in result.lines), result.lines


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
