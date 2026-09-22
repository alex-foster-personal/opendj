"""scripts/lock_metadata_check.py: what uv collapses or defaults, compared as uv does.

- [if] pyproject.toml repeats a requirement (any spelling of the same one) [then] it
  matches the single requires-dist entry uv records, exit 0, [else stop]
- [if] pyproject.toml omits requires-python and the lock records uv's interpreter
  default (`>=X.Y`) [then] exit 2 UNKNOWN naming the interpreter, never a verdict;
  any other recorded shape is a leftover declaration, exit 1, [else stop]
"""

from __future__ import annotations

from pathlib import Path

from scripts.lock_metadata_check import EXIT_OK, EXIT_STALE, EXIT_UNKNOWN
from tests.scripts.test_lock_metadata_check import LOCK, PYPROJECT, _run


def test_a_repeated_requirement_matches_the_single_entry_uv_records(tmp_path: Path) -> None:
    """uv deduplicates `project.dependencies` and an extra's list after parsing
    (`six>=1.16`, `six >= 1.16`, `Six>=1.16.0` are one entry, the last spelling
    kept) and `uv lock --check` passes on the fresh lock (measured uv 0.8.17,
    Codex P2 on #3763, round 23); a count of two here read that lock as stale."""
    repeated = PYPROJECT.replace(
        '"numpy>=1.26",', '"numpy>=1.26",\n    "numpy >= 1.26.0",\n    "Numpy>=01.26",'
    ).replace('"pytest>=9,<10",', '"pytest>=9,<10",\n    "pytest>=9.0,<10",')
    assert repeated != PYPROJECT
    code, message = _run(tmp_path, repeated, LOCK)
    assert (code, message) == (EXIT_OK, message)
    # The same requirement under another marker is still two entries, as uv records it.
    code, message = _run(
        tmp_path,
        PYPROJECT.replace('"numpy>=1.26",', '"numpy>=1.26",\n    "numpy>=1.26; extra == \'x\'",'),
        LOCK,
    )
    assert code == EXIT_STALE
    assert "in pyproject.toml, not in uv.lock: numpy>=1.26; extra == 'x'" in message


def test_an_omitted_requires_python_is_unknown_over_uv_s_default_and_stale_otherwise(
    tmp_path: Path,
) -> None:
    """uv fills an omitted requires-python with `>=X.Y` of the interpreter it discovers
    and `uv lock --check` fails under another interpreter (locked under 3.13, checked
    under 3.11: stale; measured uv 0.8.17, Codex P2 on #3763, round 23). The
    interpreter is not observed here, so that pair is UNKNOWN, never stale or clean;
    a recorded shape uv never defaults to is a declaration that was removed: stale."""
    omitted = PYPROJECT.replace('requires-python = ">=3.11"\n', "")
    assert omitted != PYPROJECT
    code, message = _run(tmp_path, omitted, LOCK)
    assert code == EXIT_UNKNOWN
    assert "interpreter" in message and "'>=3.11'" in message
    code, message = _run(tmp_path, omitted, LOCK.replace('">=3.11"', '">=3.11, <4"'))
    assert code == EXIT_STALE
    assert "requires-python: pyproject.toml None, uv.lock '>=3.11, <4'" in message
    # Another staleness beside the unobservable default is still reported as stale.
    code, message = _run(tmp_path, omitted.replace('"numpy>=1.26"', '"numpy>=2.1"'), LOCK)
    assert code == EXIT_STALE
    assert "in pyproject.toml, not in uv.lock: numpy>=2.1" in message
