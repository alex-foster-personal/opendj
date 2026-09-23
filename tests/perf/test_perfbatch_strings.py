"""PERFBATCH-07: docstring versus string content, judged on tokenize/ast facts.

Every diff is real ``git diff`` output from a disposable repository, read
through the production ``--diff`` path. The benign shape (a real docstring)
passes; every string that merely looks like one is a mutation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.perf.perfbatch_helpers import LOCAL_WORKER, STEMS_JOB, GateRepo

BUNDLE_WORKER = "scripts/stem_bundle_worker.py"
REQ = pytest.mark.requirement("PERFBATCH-07")


@pytest.fixture
def repo(tmp_path: Path) -> GateRepo:
    return GateRepo(tmp_path / "repo")


@REQ
def test_docstring_edit_after_def_needs_no_markers(repo: GateRepo) -> None:
    """[if] only docstring lines under a def header change [then] no markers needed, [else stop]."""
    before = (
        'def separate(track: str) -> int:\n    """Separate one track.\n\n'
        '    Old detail.\n    """\n    return 0\n'
    )
    repo.edit(STEMS_JOB, before, before.replace("Old detail.", "New detail, reworded."))
    rc, out, _ = repo.gate()
    assert rc == 0
    assert "docstring x2" in out


@REQ
def test_module_docstring_edit_is_benign_wherever_it_sits(repo: GateRepo) -> None:
    """[if] a module docstring changes, even behind a PEP 723 block [then] benign, [else stop]."""
    before = (
        '#!/usr/bin/env python3\n"""Stem bundle worker.\n\nOld usage line.\n"""\n\nimport sys\n'
    )
    repo.edit(BUNDLE_WORKER, before, before.replace("Old usage line.", "New usage line."))
    rc, out, _ = repo.gate()
    assert rc == 0
    assert "docstring x2" in out
    # ast knows the module docstring exactly, however far down it starts.
    deeper = (
        '#!/usr/bin/env python3\n# /// script\n# dependencies = ["demucs==4.0.1"]\n# ///\n'
        '"""Stem bundle worker.\n\nOld usage line.\n"""\n\nimport sys\n'
    )
    repo.seed({BUNDLE_WORKER: deeper})
    repo.write(BUNDLE_WORKER, deeper.replace("Old usage line.", "New usage line."))
    rc, out, _ = repo.gate()
    assert rc == 0 and "docstring x2" in out
    # A string in the same position that is NOT the first statement is not a docstring.
    listed = 'import sys\n\nPROMPTS = [\n    """Old usage line.""",\n]\n'
    repo.seed({BUNDLE_WORKER: listed})
    repo.write(BUNDLE_WORKER, listed.replace("Old usage line.", "New usage line."))
    rc, _, err = repo.gate()
    assert rc == 1 and "inside a string literal" in err
    # A docstring sharing its closing line with a statement (Codex P1 at
    # 7419497d7) is refused even when only the statement changes.
    shared = 'def f() -> int:\n    """doc"""; workers = 2\n    return workers\n'
    repo.seed({BUNDLE_WORKER: shared})
    repo.write(BUNDLE_WORKER, shared.replace("workers = 2", "workers = 8"))
    rc, _, err = repo.gate()
    assert rc == 1 and "inside a string literal" in err
    # A trailing comment on the closing line does not demote the docstring.
    commented = 'def f() -> int:\n    """doc"""  # old note\n    return 0\n'
    repo.seed({BUNDLE_WORKER: commented})
    repo.write(BUNDLE_WORKER, commented.replace("doc", "doc, reworded"))
    rc, out, _ = repo.gate()
    assert rc == 0 and "docstring x2" in out


@REQ
def test_triple_quoted_prompt_is_not_a_docstring(repo: GateRepo) -> None:
    """[if] a triple-quoted string is not a docstring [then] its edit is a mutation, [else stop]."""
    before = 'PROMPT = """\nAlign these lyrics.\n"""\n'
    repo.edit("apps/lyrics/align.py", before, before.replace("lyrics.", "lyrics, fast."))
    rc, _, err = repo.gate()
    assert rc == 1 and "'Align these lyrics.' inside a string literal" in err


@REQ
def test_long_docstring_edit_far_from_its_edges_is_benign(repo: GateRepo) -> None:
    """[if] a docstring line changes with both edges outside the hunk [then] benign, [else stop]."""
    body = "".join(f"    line {n} of prose.\n" for n in range(12))
    before = f'def f() -> None:\n    """Long.\n\n{body}    """\n'
    repo.edit(STEMS_JOB, before, before.replace("line 6 of prose.", "line six of prose."))
    rc, out, _ = repo.gate()
    assert rc == 0 and "docstring x2" in out
    # The same edit inside a long non-docstring string is a mutation.
    prompt = f'PROMPT = (\n    """Long.\n\n{body}    """\n)\n'
    repo.seed({STEMS_JOB: prompt})
    repo.write(STEMS_JOB, prompt.replace("line 6 of prose.", "line six of prose."))
    rc, _, err = repo.gate()
    assert rc == 1 and "inside a string literal" in err
    # A file that does not tokenize on either side refuses the file outright.
    broken = "def f(:\n    pass\n# note\n"
    repo.seed({STEMS_JOB: broken})
    repo.write(STEMS_JOB, broken + "# another note\n")
    rc, _, err = repo.gate()
    assert rc == 1 and "cannot tokenize" in err


@REQ
def test_docstring_closer_followed_by_code_is_not_a_docstring(repo: GateRepo) -> None:
    """[if] a triple-quoted closer is followed by code [then] it is a mutation, [else stop]."""
    one_line = 'def f() -> str:\n    """doc""".format(1)\n    return ""\n'
    repo.edit(STEMS_JOB, one_line, one_line.replace("doc", "doc2"))
    rc, _, _ = repo.gate()
    assert rc == 1
    multi = 'def g() -> str:\n    """doc\n    old\n    """ + compute()\n'
    repo.seed({STEMS_JOB: multi})
    repo.write(STEMS_JOB, multi.replace("old", "new"))
    rc, _, _ = repo.gate()
    assert rc == 1


@REQ
def test_code_shaped_text_inside_a_string_is_never_paired(repo: GateRepo) -> None:
    """[if] except or import text inside a string changes [then] it is a mutation, [else stop]."""
    prompt = 'PROMPT = """\nUse this shape:\n    except OSError:\n        pass\n"""\n'
    repo.edit(
        "apps/lyrics/align.py",
        prompt,
        prompt.replace("except OSError:", "except (OSError, ValueError):"),
    )
    rc, _, err = repo.gate()
    assert rc == 1 and "inside a string literal" in err
    guard = 'DOC = """\nimport fcntl\n"""\n'
    repo.seed({LOCAL_WORKER: guard})
    repo.write(
        LOCAL_WORKER,
        guard.replace(
            "import fcntl\n", "try:\n    import fcntl\nexcept ImportError:\n    fcntl = None\n"
        ),
    )
    rc, _, err = repo.gate()
    assert rc == 1 and "inside a string literal" in err
    # An escaped delimiter never closes the literal (Codex P1 at 8b02166b3):
    # the except line after it is still string content.
    escaped = 'PROMPT = """\nexample \\""" does not close\n    except OSError:\n"""\n'
    repo.seed({"apps/lyrics/align.py": escaped})
    repo.write(
        "apps/lyrics/align.py",
        escaped.replace("except OSError:", "except (OSError, ValueError):"),
    )
    rc, _, err = repo.gate()
    assert rc == 1 and "inside a string literal" in err
    # An ordinary literal holding three quote characters right before a
    # prompt (Codex P1 at 07f75d17e): tokenize sees one STRING then another.
    quoted = 'X = \'"""\'\nPROMPT = """\n    except OSError:\n"""\n'
    repo.seed({"apps/lyrics/align.py": quoted})
    repo.write(
        "apps/lyrics/align.py",
        quoted.replace("except OSError:", "except (OSError, ValueError):"),
    )
    rc, _, err = repo.gate()
    assert rc == 1 and "inside a string literal" in err
    # A region that closes and another that opens on the same line
    # (Codex P1 at 939ffbc95): the second region is still string content.
    joined = 'X = """\na\n""" + """\n    except OSError:\n"""\n'
    repo.seed({"apps/lyrics/align.py": joined})
    repo.write(
        "apps/lyrics/align.py",
        joined.replace("except OSError:", "except (OSError, ValueError):"),
    )
    rc, _, err = repo.gate()
    assert rc == 1 and "inside a string literal" in err
    # Same shape with the def-and-docstring on one side: still never paired.
    joined_doc = 'def f():\n    """doc""" + """\n    except OSError:\n"""\n'
    repo.seed({STEMS_JOB: joined_doc})
    repo.write(STEMS_JOB, joined_doc.replace("except OSError:", "except (OSError, ValueError):"))
    rc, _, err = repo.gate()
    assert rc == 1 and "inside a string literal" in err
