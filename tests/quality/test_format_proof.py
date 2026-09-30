"""A format-only commit must be PROVEN format-only, and blame must keep skipping it.

Issue #4456 lands a whole-tree formatting pass in parts. Two things can quietly
go wrong, and each has its own command in `scripts/format_proof.py`:

  - `prove` compares every changed Python file's AST before and after, with
    docstring whitespace normalized the way Black's own safety check does it.
    A "format" commit that also changed a value, renamed a name or added a
    file is a real edit, and blame must not be told to skip it.
  - `ignore-revs` checks `.git-blame-ignore-revs`; its tests live in
    `test_format_proof_ignore_revs.py`, and both share `format_proof_repo.py`.

Every test builds its own throwaway git repository, so nothing here depends on
this checkout's history or on whether CI cloned it shallow.

Regression lines:
  - if prove passes a commit that changed a value then broken
  - if prove fails a pure re-wrap or a docstring-whitespace-only change then broken
  - if prove passes a changed docstring's TEXT then broken
  - if prove reports a pass when a file does not parse, or when no Python changed, then broken
  - if prove passes a commit that added, deleted or renamed a file then broken
  - if prove passes a range that also edits a file that is not Python then broken
  - if prove passes a symlink or submodule named .py, or fails a reformatted executable .py file, then broken
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
  - if prove passes a comment moved between the pieces of one implicitly concatenated string then broken
  - if prove fails ruff joining string pieces around a comment, or keeping one between them, then broken
  - if prove passes ruff moving a trailing operator past an end-of-line comment then broken: it is not provable
  - if prove certifies a comment moved across an operator inside an f-string field then broken
  - if prove fails ruff re-laying a commented call inside an f-string field then broken
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
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from scripts import format_proof
from tests.quality.format_proof_repo import IDENTITY, commit_files, commit_index_entries, hash_blob, init_repo, run_git

# PEP 701: a comment inside an f-string's replacement field parses only on 3.12 and later.
FIELD_COMMENTS_PARSE = sys.version_info >= (3, 12)

UNFORMATTED = 'def f(a,b):\n    """Add two.   \n\n       Returns the sum."""\n    return a+b\n'
REFORMATTED = 'def f(a, b):\n    """Add two.\n\n    Returns the sum."""\n    return a + b\n'
IGNORE_BEFORE_REWRAP = "def f(a,\n      b):\n    return a+b\ny = f(1, 2)  # type: ignore[call-arg]\n"
IGNORE_AFTER_REWRAP = "def f(a, b):\n    return a + b\ny = f(1, 2)  # type: ignore[call-arg]\n"


# ----- prove ----------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo")


def test_prove_passes_a_pure_reformat(repo: Path) -> None:
    base = commit_files(repo, {"m.py": "x=[1,2 ,3]\n"}, "init")
    head = commit_files(repo, {"m.py": "x = [1, 2, 3]\n"}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 0, result.lines
    assert result.files_checked == 1


def test_prove_accepts_docstring_whitespace_only(repo: Path) -> None:
    base = commit_files(repo, {"m.py": UNFORMATTED}, "init")
    head = commit_files(repo, {"m.py": REFORMATTED}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 0, result.lines
    assert result.docstring_normalized == 1


def test_prove_rejects_a_changed_value(repo: Path) -> None:
    base = commit_files(repo, {"m.py": "LIMIT=600\n"}, "init")
    head = commit_files(repo, {"m.py": "LIMIT = 601\n"}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 1
    assert any("m.py" in line for line in result.lines)


def test_prove_rejects_changed_docstring_text(repo: Path) -> None:
    base = commit_files(repo, {"m.py": '"""Old words."""\n'}, "init")
    head = commit_files(repo, {"m.py": '"""New words."""\n'}, "style(format): ruff@0.16.3 m")
    assert format_proof.prove(repo, base, head).exit_code == 1


def test_prove_is_unknown_when_a_file_does_not_parse(repo: Path) -> None:
    base = commit_files(repo, {"m.py": "x = 1\n"}, "init")
    head = commit_files(repo, {"m.py": "x = (\n"}, "style(format): ruff@0.16.3 m")
    assert format_proof.prove(repo, base, head).exit_code == 2


def test_prove_is_unknown_when_no_python_changed(repo: Path) -> None:
    base = commit_files(repo, {"m.py": "x = 1\n", "README.md": "a\n"}, "init")
    head = commit_files(repo, {"README.md": "b\n"}, "docs: readme")
    assert format_proof.prove(repo, base, head).exit_code == 2


def test_prove_rejects_an_added_file(repo: Path) -> None:
    base = commit_files(repo, {"m.py": "x=1\n"}, "init")
    head = commit_files(repo, {"m.py": "x = 1\n", "n.py": "y = 2\n"}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 1
    assert any("n.py" in line for line in result.lines)


def test_prove_is_unknown_when_a_non_python_file_also_changed(repo: Path) -> None:
    """No AST can check a TOML edit, so it must never ride along in a proven format commit."""
    base = commit_files(repo, {"m.py": "x=1\n", "pyproject.toml": "a = 1\n"}, "init")
    head = commit_files(repo, {"m.py": "x = 1\n", "pyproject.toml": "a = 2\n"}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 2
    assert any("pyproject.toml" in line for line in result.lines)


@pytest.mark.parametrize(
    ("mode", "before", "after"),
    [("120000", "a", "(a)"), ("160000", "1" * 40, "2" * 40)],
    ids=["symlink", "gitlink"],
)
def test_prove_is_unknown_when_a_py_path_is_not_a_regular_file(repo: Path, mode: str, before: str, after: str) -> None:
    """A symlink's targets `a` and `(a)` parse to one AST yet name different files, and a gitlink is a submodule
    commit. Neither is Python source, so a real reformat beside one cannot make the range proven."""
    objects = (before, after) if mode == "160000" else (hash_blob(repo, before), hash_blob(repo, after))
    base = commit_index_entries(
        repo, {"n.py": ("100644", hash_blob(repo, "x=1\n")), "m.py": (mode, objects[0])}, "init"
    )
    head = commit_index_entries(
        repo, {"n.py": ("100644", hash_blob(repo, "x = 1\n")), "m.py": (mode, objects[1])}, "style(format): ruff m"
    )
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 2, result.lines
    assert any("m.py" in line and "not a regular .py file" in line for line in result.lines), result.lines


def test_prove_control_an_executable_py_file_still_proves(repo: Path) -> None:
    """Opposite-direction control: mode 100755 is a regular file too, and every script with a shebang has it."""
    base = commit_index_entries(repo, {"m.py": ("100755", hash_blob(repo, UNFORMATTED))}, "init")
    head = commit_index_entries(
        repo, {"m.py": ("100755", hash_blob(repo, REFORMATTED))}, "style(format): ruff@0.16.3 m"
    )
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 0, result.lines


def test_prove_still_fails_a_python_edit_beside_a_non_python_file(repo: Path) -> None:
    base = commit_files(repo, {"m.py": "x=1\n", "pyproject.toml": "a = 1\n"}, "init")
    head = commit_files(repo, {"m.py": "x = 2\n", "pyproject.toml": "a = 2\n"}, "style(format): ruff@0.16.3 m")
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
    base = commit_files(repo, {"m.py": before}, "init")
    head = commit_files(repo, {"m.py": after}, "style(format): ruff@0.16.3 m")
    assert format_proof.prove(repo, base, head).exit_code == 1


def test_prove_control_a_rewrap_may_move_a_type_ignore_to_another_line(repo: Path) -> None:
    """Opposite-direction control: an ignore's LINE is layout, so a re-wrap above it still proves."""
    base = commit_files(repo, {"m.py": IGNORE_BEFORE_REWRAP}, "init")
    head = commit_files(repo, {"m.py": IGNORE_AFTER_REWRAP}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 0, result.lines


def test_prove_rejects_a_mode_change(repo: Path) -> None:
    """A chmod-only change is status M with identical ASTs; dropping an executable bit is not layout."""
    base = commit_files(repo, {"m.py": "x = 1\n"}, "init")
    run_git(repo, "update-index", "--chmod=+x", "m.py")
    run_git(repo, *IDENTITY, "commit", "-q", "-m", "style(format): chmod")
    result = format_proof.prove(repo, base, run_git(repo, "rev-parse", "HEAD"))
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
    base = commit_files(repo, {"m.py": before}, "init")
    head = commit_files(repo, {"m.py": after}, "style(format): ruff@0.16.3 m")
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
        ('x = (\n    # c\n    "a"\n    "b"\n)\n', 'x = (\n    # c\n    "ab"\n)\n'),
        ('x = ("a"  # c\n     "b")\n', 'x = (\n    "a"  # c\n    "b"\n)\n'),
        ('a = ("x"\n     "y")\nb = (\n    "p"  # c\n    "q"\n)\n', 'a = "xy"\nb = (\n    "p"  # c\n    "q"\n)\n'),
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
        "string-pieces-joined-below-a-comment",
        "string-pieces-kept-around-a-comment",
        "string-pieces-joined-above-a-kept-run",
    ],
)
def test_prove_control_layout_around_comments_still_proves(repo: Path, before: str, after: str) -> None:
    """Opposite-direction control: a comment's line and spacing are layout; only its statement and words count."""
    base = commit_files(repo, {"m.py": before}, "init")
    head = commit_files(repo, {"m.py": after}, "style(format): ruff@0.16.3 m")
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
        ('x = (\n    "a"  # noqa: E501\n    "b"\n    "c"\n)\n', 'x = (\n    "a"\n    "b"  # noqa: E501\n    "c"\n)\n'),
        ('x = (\n    "a"\n    # c\n    "b"\n    "c"\n)\n', 'x = (\n    "a"\n    "b"\n    # c\n    "c"\n)\n'),
        ('x = (\n    f"a"  # c\n    f"b"\n    "c"\n)\n', 'x = (\n    f"a"\n    f"b"  # c\n    "c"\n)\n'),
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
        "between-string-pieces",
        "own-line-between-string-pieces",
        "between-f-string-pieces",
    ],
)
def test_prove_rejects_a_comment_moved_to_another_place_in_the_tree(repo: Path, before: str, after: str) -> None:
    """A comment keeps how many AST nodes open and close before it, how many of the tokens ruff never adds or drops
    precede it (names, keywords, numbers, operators: `else:` and `+` have no node of their own), and whether it ends
    a line of code. Some cases cross exactly one of those: a node start (into-a-parenthesized-tuple, whose `(` ruff
    may drop), a node end (out-of-a-nested-call), a keyword (across-a-keyword-operator), an operator
    (own-line-across-an-operator), a string piece (between-string-pieces, which one node spans), a line end
    (header-pragma-into-the-body). A whole-module type-ignore only works
    above the first statement, and coverage reads `if cond:  # pragma: no cover` as the whole block, a comment-only
    line as nothing."""
    base = commit_files(repo, {"m.py": before}, "init")
    head = commit_files(repo, {"m.py": after}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 1
    assert any("comment" in line for line in result.lines), result.lines


def test_prove_fails_safe_when_ruff_moves_a_trailing_operator_past_a_comment(repo: Path) -> None:
    """The rule's known limit, pinned so it is not loosened by accident: ruff turns `a +  # c` then `b` into `a  # c`
    then `+ b`, which moves the operator past the comment. No token count tells that from a comment moved across the
    operator, so the proof fails it, and such a comment is re-anchored in its own commit before the part."""
    base = commit_files(repo, {"m.py": "x = (a +  # c\n     b)\n"}, "init")
    head = commit_files(repo, {"m.py": "x = (\n    a  # c\n    + b\n)\n"}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 1
    assert any("comment" in line for line in result.lines), result.lines


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ('x = f"{a +  # c\n    b}"\n', 'x = f"{a  # c\n    + b}"\n'),
        ('x = f"{\n    a\n    # c\n    + b\n}"\n', 'x = f"{\n    a +\n    # c\n    b\n}"\n'),
    ],
    ids=["end-of-line-across-a-plus", "own-line-across-a-plus"],
)
def test_prove_never_certifies_a_comment_moved_inside_an_f_string_field(repo: Path, before: str, after: str) -> None:
    """On 3.12 and later a replacement field's names and operators are tokens of their own and count like any other,
    so the move fails. Before 3.12 a comment cannot sit in a field at all: no AST reads the file, so it is UNKNOWN."""
    base = commit_files(repo, {"m.py": before}, "init")
    head = commit_files(repo, {"m.py": after}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == (1 if FIELD_COMMENTS_PARSE else 2), result.lines


def test_prove_control_ruff_expanding_a_commented_call_inside_an_f_string_field_still_proves(repo: Path) -> None:
    """Opposite-direction control, ruff 0.16.3's own output: counting a field's tokens does not fail its re-layout."""
    base = commit_files(repo, {"m.py": 'x = f"{foo(a,  # c\n    b)}"\n'}, "init")
    after = 'x = f"{\n    foo(\n        a,  # c\n        b,\n    )\n}"\n'
    head = commit_files(repo, {"m.py": after}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == (0 if FIELD_COMMENTS_PARSE else 2), result.lines


def test_prove_control_a_semicolon_split_keeps_the_trailing_comment(repo: Path) -> None:
    """Opposite-direction control: `x = 1; y = 2  # c` annotates y, and ruff's split leaves it there."""
    base = commit_files(repo, {"m.py": "x = 1; y = 2  # c\n"}, "init")
    head = commit_files(repo, {"m.py": "x = 1\ny = 2  # c\n"}, "style(format): ruff@0.16.3 m")
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
    base = commit_files(repo, {"m.py": before}, "init")
    head = commit_files(repo, {"m.py": after}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 1
    assert any("shebang" in line for line in result.lines), result.lines


def test_prove_decodes_each_side_by_its_own_cookie(repo: Path) -> None:
    """Python honors a coding cookie only on line 1 or 2. Moved out of reach, the same bytes decode to another
    string, which only a per-side decode can see: a utf-8 read of both sides finds them equal."""
    before = b'# -*- coding: latin-1 -*-\nx = "\xc3\xa9"\n'
    after = b'\n\n# -*- coding: latin-1 -*-\nx = "\xc3\xa9"\n'
    base = commit_files(repo, {"m.py": before}, "init")
    head = commit_files(repo, {"m.py": after}, "style(format): ruff@0.16.3 m")
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
    base = commit_files(repo, {"m.py": before}, "init")
    head = commit_files(repo, {"m.py": after}, "style(format): ruff@0.16.3 m")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 0, result.lines


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


def test_prove_control_the_normalizer_does_not_hide_a_real_string_edit(repo: Path) -> None:
    """Opposite-direction control: whitespace inside a NON-docstring string is data."""
    base = commit_files(repo, {"m.py": 'def f():\n    x = "a  "\n    return x\n'}, "init")
    head = commit_files(repo, {"m.py": 'def f():\n    x = "a"\n    return x\n'}, "style(format): ruff@0.16.3 m")
    assert format_proof.prove(repo, base, head).exit_code == 1
