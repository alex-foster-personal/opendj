"""Value types, functions and enums that leak open types through members or nesting."""

from __future__ import annotations

from pathlib import Path


def _load_fragment(name: str) -> None:
    """Load a sibling fragment with real source so ``inspect.getsource`` can read handlers."""
    path = Path(__file__).resolve().parent.parent / f"leak_{name}.py"
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), globals(), globals())  # noqa: S102


_load_fragment("shared")
_load_fragment("bindings")
_load_fragment("annotated")
_load_fragment("introspection")
