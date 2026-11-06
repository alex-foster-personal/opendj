"""The signing --keychain half of INSTALL-04, split out of test_macos_signing.py.

Optional MDT_MACOS_SIGNING_KEYCHAIN passes --keychain to every codesign that
carries --sign so identity lookup does not follow the ambient search list
exclusively (host-free checks; the live path needs the macOS toolchain).

Split into its own file rather than appended to test_macos_signing.py so that
file stays under the repo's 600-line-per-test-file ratchet. Imports `_sign`
and `UNRESOLVABLE_IDENTITY` from that module rather than duplicating them.

Single-line acceptance checks, in the repo's "if X then Y" shape:

- if payload resolves a signing keychain path that is not a file, codesign
  runs and fails opaquely later instead of refusing up front -> broken.
- if any codesign invocation that carries --sign omits the signing keychain
  argv expansion, CI cannot catch regressions -> broken.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from pathlib import Path

import pytest

from tests.scripts.test_macos_signing import SIGN_SCRIPT, UNRESOLVABLE_IDENTITY, _sign

_SIGN_LINES_NEED = (
    '${SIGNING_KEYCHAIN_ARGS[@]+"${SIGNING_KEYCHAIN_ARGS[@]}"}',
)

# _sign() passes the .sh script to subprocess.run; Windows cannot execute that
# shebang before the keychain assertions.
needs_posix_shell = pytest.mark.skipif(
    sys.platform == "win32",
    reason="sign_macos_developer_id.sh is a POSIX shebang script; Windows cannot execute it",
)

# ----- recording codesign (refuse before any codesign call) -------------


def _recording_codesign_path(tmp_path: Path) -> tuple[Path, Path]:
    """A PATH dir whose codesign records argv; must never run on refuse paths."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "codesign.calls"
    codesign = bin_dir / "codesign"
    codesign.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(calls))}\n"
        "exit 0\n"
    )
    codesign.chmod(0o755)
    return bin_dir, calls


@pytest.mark.requirement("INSTALL-04")
@needs_posix_shell
def test_payload_refuses_a_signing_keychain_that_is_not_a_file(tmp_path: Path) -> None:
    """[if] payload uses a signing keychain path that is not a file [then] refuse before codesign, [else stop]."""
    missing = tmp_path / "absent.keychain-db"
    bin_dir, calls = _recording_codesign_path(tmp_path)
    result = _sign(
        "payload",
        str(tmp_path),
        PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        MDT_MACOS_SIGNING_IDENTITY=UNRESOLVABLE_IDENTITY,
        MDT_MACOS_SIGNING_KEYCHAIN=str(missing),
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "MDT_MACOS_SIGNING_KEYCHAIN" in combined
    assert str(missing) in combined
    if calls.exists():
        assert calls.read_text() == "", calls.read_text()


# ----- static: every codesign ... --sign expands the keychain argv ---------


def _collapse_bash_line_continuations(text: str) -> list[str]:
    """Join \\ continuation lines so one logical shell line is one string."""
    physical = text.splitlines()
    out: list[str] = []
    acc: list[str] = []
    for line in physical:
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            acc.append(stripped[:-1].rstrip())
        else:
            acc.append(line)
            out.append(" ".join(acc))
            acc = []
    if acc:
        out.append(" ".join(acc))
    return out


def _codesign_lines_with_sign(collapsed_lines: list[str]) -> list[str]:
    return [ln for ln in collapsed_lines if re.search(r"\bcodesign\b", ln) and "--sign" in ln]


@pytest.mark.requirement("INSTALL-04")
def test_every_codesign_that_carries_sign_expands_the_signing_keychain_argv() -> None:
    """[if] codesign --sign omits SIGNING_KEYCHAIN_ARGS [then] wrong keychain lookup, [else stop]."""
    source = SIGN_SCRIPT.read_text(encoding="utf-8", errors="replace")
    collapsed = _collapse_bash_line_continuations(source)
    sign_lines = _codesign_lines_with_sign(collapsed)
    assert len(sign_lines) == 3, "\n".join(sign_lines)
    for line in sign_lines:
        for needle in _SIGN_LINES_NEED:
            assert needle in line, line
        assert "--keychain-profile" not in line, line

    assert any("codesign --verify" in ln for ln in collapsed), collapsed
    assert any(re.search(r"\bcodesign\b", ln) and " -d " in ln for ln in collapsed), collapsed
