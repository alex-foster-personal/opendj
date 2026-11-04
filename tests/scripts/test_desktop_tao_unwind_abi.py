"""INSTALL-8330: the macOS event loop must let an exception unwind, not abort.

Open DJ.app (build 435b7f0d) aborted on Tue 3 Nov 2026 05:50Z mid set prep:
SIGABRT from `panic_cannot_unwind`, called from tao's `sendEvent:` override
straight under `-[NSApplication _handleEvent:]`. tao 0.35.3 declared that
override, and its other AppKit callbacks, `extern "C"`; since Rust 1.81 an
exception or panic unwinding through such a frame aborts the process. tao
0.37.2 (pulled by tauri 2.12) declares them `extern "C-unwind"`.

- if the shell's lockfile resolves tao below 0.37.2 then an AppKit exception
  during any mouse or key event kills Open DJ mid set again -> broken

-Claude
"""

from __future__ import annotations

import tomllib
from pathlib import Path

LOCKFILE = Path(__file__).resolve().parents[2] / "apps" / "desktop" / "src-tauri" / "Cargo.lock"
FIRST_UNWIND_SAFE_TAO = (0, 37, 2)


def _locked_versions(name: str) -> list[tuple[int, ...]]:
    packages = tomllib.loads(LOCKFILE.read_text(encoding="utf-8"))["package"]
    return [tuple(int(p) for p in pkg["version"].split(".")) for pkg in packages if pkg["name"] == name]


def test_shell_resolves_exactly_one_tao() -> None:
    assert len(_locked_versions("tao")) == 1, "two tao versions would put the old extern C callbacks back in the binary"


def test_shell_tao_declares_appkit_callbacks_c_unwind() -> None:
    (version,) = _locked_versions("tao")
    assert version >= FIRST_UNWIND_SAFE_TAO, (
        f"tao {'.'.join(map(str, version))} declares sendEvent: extern \"C\": an AppKit exception aborts Open DJ"
    )
