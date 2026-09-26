"""Refuse to treat a protected library root as a disposable fixture directory.

Fixture builders delete and rewrite their target directory. Call
:func:`refuse_protected_target` BEFORE any ``rmtree`` or write, so a mistyped
``--data-dir`` or env var fails fast instead of wiping a real library. The
protected roots are fixed paths that do not depend on ``MDT_DATA_DIR``: a fixture
builder usually runs with ``MDT_DATA_DIR`` pointed at its own target, so a guard
derived from it would compare the target with itself. For the same reason the home
directory is the account's own (``pwd``), not ``$HOME``: e2e servers run with ``HOME``
pointed at a sandbox inside the fixture dir.
"""

from __future__ import annotations

import os
from pathlib import Path

from apps.shared.paths import PROJECT_ROOT


def _account_home() -> Path:
    if os.name == "nt":
        return Path.home()
    import pwd

    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def protected_roots() -> tuple[Path, ...]:
    home = _account_home()
    roots = [
        PROJECT_ROOT / "data",
        home / "Library" / "Pioneer",
        home / "Library" / "Application Support" / "com.opendj.desktop",
    ]
    if os.name == "nt":
        # Windows keeps the live rekordbox tree and the Tauri app data under
        # %APPDATA% (apps.shared.platform_paths); fail fast rather than guess it.
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise SystemExit("[ERROR] APPDATA is unset; cannot locate the live library roots")
        roots += [Path(appdata) / "Pioneer", Path(appdata) / "com.opendj.desktop"]
    return tuple(roots)


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
