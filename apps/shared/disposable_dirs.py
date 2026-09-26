"""Refuse to treat a protected library root as a disposable fixture directory.

Fixture builders delete and rewrite their target directory. Call
:func:`refuse_protected_target` BEFORE any ``rmtree`` or write, so a mistyped
``--data-dir`` or env var fails fast instead of wiping a real library. The
protected roots are fixed paths that do not depend on ``MDT_DATA_DIR``: a fixture
builder usually runs with ``MDT_DATA_DIR`` pointed at its own target, so a guard
derived from it would compare the target with itself.
"""

from __future__ import annotations

from pathlib import Path

from apps.shared.paths import PROJECT_ROOT


def protected_roots() -> tuple[Path, ...]:
    home = Path.home()
    return (
        PROJECT_ROOT / "data",
        home / "Library" / "Pioneer",
        home / "Library" / "Application Support" / "com.opendj.desktop",
    )


def refuse_protected_target(target: Path, *extra_protected: Path) -> None:
    """Raise SystemExit if ``target`` is, sits inside, or contains a protected root."""
    resolved = target.resolve()
    for root in (*protected_roots(), *extra_protected):
        protected = root.resolve()
        if resolved == protected or protected in resolved.parents or resolved in protected.parents:
            raise SystemExit(
                f"[ERROR] refusing to use {resolved} as a disposable fixture dir: "
                f"it overlaps the protected root {protected}"
            )
