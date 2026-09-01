"""Fail loudly when the running interpreter violates ``requires-python``.

Requirements:

- ✔︎ The floor is read from ``pyproject.toml``, never hardcoded here.
- ✔︎ An interpreter below the floor exits non-zero naming both versions.
- ✔︎ A specifier this module cannot parse fails; it never passes by default.

Acceptance tests:

- [if] the running interpreter is below the declared floor [then ⛔️] the
  check exits 0.
- [if] ``requires-python`` is missing or is not a bare ``>=X.Y`` floor
  [then ⛔️] the check passes it through unverified.
- [if] the interpreter satisfies the floor [then ⛔️] the check fails.

WHY THIS EXISTS: ``uv run <command>`` resolves a command that is absent from
the project environment off ``PATH`` instead, and a ``PATH`` command carries
its own interpreter with it. ``pytest`` lives in this project's ``dev`` extra,
so a plain ``uv sync`` leaves it out and ``uv run pytest`` silently executes
under, for example, homebrew's Python 3.10 while ``pyproject.toml`` declares
``requires-python = ">=3.11"``. uv does not warn. The visible symptom is a
confusing ``ImportError: cannot import name 'UTC' from 'datetime'`` at
collection rather than a statement that the interpreter is wrong.

STDLIB ONLY, AND PYTHON 3.8 COMPATIBLE ON PURPOSE: this module has to import
and report under the very interpreters it rejects, so it cannot use
``tomllib`` (3.11+), ``packaging`` (not always installed), or any 3.10+
syntax. Keep it that way.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

REQUIRES_PYTHON_PATTERN = re.compile(
    r"""^[ \t]*requires-python[ \t]*=[ \t]*["']([^"']+)["']""",
    re.MULTILINE,
)
FLOOR_PATTERN = re.compile(r"^>=[ \t]*(\d+)\.(\d+)(?:\.(\d+))?$")

DEFAULT_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


class InterpreterContractError(RuntimeError):
    """The running interpreter does not satisfy the declared floor."""


def read_requires_python(pyproject_path: Path) -> str:
    """Return the raw ``requires-python`` specifier declared by the project."""
    if not pyproject_path.is_file():
        raise InterpreterContractError(
            f"no pyproject.toml at {pyproject_path}, so the interpreter floor is "
            "unknown and cannot be checked"
        )
    text = pyproject_path.read_text(encoding="utf-8")
    match = REQUIRES_PYTHON_PATTERN.search(text)
    if match is None:
        raise InterpreterContractError(
            f"{pyproject_path} declares no requires-python, so there is no floor to enforce"
        )
    return match.group(1).strip()


def parse_floor(specifier: str) -> tuple[int, ...]:
    """Parse a bare ``>=X.Y`` floor, refusing anything richer."""
    match = FLOOR_PATTERN.match(specifier.strip())
    if match is None:
        raise InterpreterContractError(
            f"requires-python {specifier!r} is not a bare '>=X.Y' floor, which is the "
            "only form this check understands. Widen the parser rather than "
            "letting an unchecked interpreter through."
        )
    return tuple(int(part) for part in match.groups() if part is not None)


def assert_interpreter_satisfies_floor(
    pyproject_path: Path = DEFAULT_PYPROJECT,
    version_info: Sequence[int] | None = None,
    executable: str | None = None,
) -> tuple[int, ...]:
    """Raise unless the running interpreter meets the project's floor."""
    floor = parse_floor(read_requires_python(pyproject_path))
    running_all = tuple(sys.version_info[:3] if version_info is None else version_info)
    running = running_all[: len(floor)]
    if running >= floor:
        return floor
    raise InterpreterContractError(
        "interpreter {} is Python {}, below the {} floor that {} "
        "declares. `uv run` falls back to a PATH command when that command "
        "is missing from the project environment, and the PATH command "
        "brings its own interpreter; `pytest` lives in the `dev` extra, so a "
        "plain `uv sync` leaves it out. Install it with "
        "`uv sync --extra dev` and re-run.".format(
            sys.executable if executable is None else executable,
            ".".join(str(part) for part in running_all),
            ">=" + ".".join(str(part) for part in floor),
            pyproject_path,
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.interpreter_contract",
        description="fail when the running interpreter violates requires-python",
    )
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=DEFAULT_PYPROJECT,
        help="pyproject.toml declaring the floor (default: this repo's)",
    )
    args = parser.parse_args(argv)
    try:
        floor = assert_interpreter_satisfies_floor(args.pyproject)
    except InterpreterContractError as exc:
        sys.stderr.write(f"[ERROR] {exc}\n")
        return 2
    print(
        "[OK] Python {} satisfies >={} ({})".format(
            ".".join(str(part) for part in sys.version_info[:3]),
            ".".join(str(part) for part in floor),
            sys.executable,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
