"""INSTALL-33: the signed desktop app may use the microphone.

Hit live on demon-llama, Fri 2 Oct 2026, desktop app 0.1.5: clicking I/O raised
"headphone output acquisition failed: The request is not allowed by the user
agent or the platform in the current context" and macOS never showed its
microphone prompt. The app is signed with the hardened runtime and its
signature carried no entitlements at all, so macOS refused the microphone
before asking. Without the microphone WebKit withholds device names, so the
I/O lists cannot be named and cue latency calibration cannot listen.

These run the real signing script against a stub `codesign` that prints a
chosen entitlements dump, so they need no certificate and no Mac.

Single-line acceptance checks, in the repo's "if X then broken" shape:

- if Entitlements.app.plist loses com.apple.security.device.audio-input, or
  tauri.conf.json stops pointing the bundler at it -> broken.
- if `verify-app-entitlements` passes a signature without the key, the gate
  reads an unentitled app as fine -> broken.
- if it passes when `codesign -d` itself fails, an unreadable signature is
  reported as a verdict -> broken.
- if `notarize-app` submits an unentitled app, Apple accepts it and the user
  still cannot name a device -> broken.
"""

from __future__ import annotations

import json
import os
import plistlib
import shlex
import subprocess
from pathlib import Path

import pytest

from tests.scripts.test_macos_signing import SIGN_SCRIPT, UNRESOLVABLE_IDENTITY, _sign

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
TAURI_DIR: Path = REPO_ROOT / "apps/desktop/src-tauri"
APP_ENTITLEMENTS: Path = TAURI_DIR / "Entitlements.app.plist"
MIC = "com.apple.security.device.audio-input"

_ENTITLED_DUMP = (
    f'<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>{MIC}</key><true/></dict></plist>'
)
# What the shipped 0.1.5 app reported: a signature with no entitlements blob.
_EMPTY_DUMP = ""


def _stub_toolchain(tmp_path: Path, *, dump: str, codesign_exit: int = 0) -> tuple[Path, Path]:
    """A PATH dir with a codesign that prints `dump`, and a recording xcrun."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    dump_file = tmp_path / "entitlements.dump"
    dump_file.write_text(dump)
    calls = tmp_path / "xcrun.calls"
    tools = {
        "codesign": f"#!/bin/sh\ncat {shlex.quote(str(dump_file))}\nexit {codesign_exit}\n",
        "xcrun": (
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> {shlex.quote(str(calls))}\n"
            'case "$1 $2" in "notarytool submit") echo "  status: Accepted";; esac\n'
            "exit 0\n"
        ),
        "ditto": '#!/bin/sh\nfor last; do :; done\n: > "$last"\n',
        "spctl": "#!/bin/sh\nexit 0\n",
    }
    for name, body in tools.items():
        tool = bin_dir / name
        tool.write_text(body)
        tool.chmod(0o755)
    return bin_dir, calls


def _app(tmp_path: Path) -> Path:
    app = tmp_path / "Open DJ.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    return app


def _path_with(bin_dir: Path) -> str:
    return f"{bin_dir}{os.pathsep}{os.environ['PATH']}"


@pytest.mark.requirement("INSTALL-33")
def test_app_entitlements_grant_exactly_the_microphone() -> None:
    """[if] Entitlements.app.plist is anything but the microphone key [then] fail, [else stop].

    Exactly one key: anything wider is surface the app has not been shown to need."""
    assert plistlib.loads(APP_ENTITLEMENTS.read_bytes()) == {MIC: True}


@pytest.mark.requirement("INSTALL-33")
def test_the_bundler_signs_the_app_with_those_entitlements() -> None:
    """[if] tauri.conf.json stops pointing the bundler at Entitlements.app.plist [then] fail, [else stop]."""
    conf = json.loads((TAURI_DIR / "tauri.conf.json").read_text())
    named = conf["bundle"]["macOS"]["entitlements"]
    assert (TAURI_DIR / named).resolve() == APP_ENTITLEMENTS.resolve()


@pytest.mark.requirement("INSTALL-33")
def test_the_app_store_build_also_asks_for_the_microphone() -> None:
    """[if] the App Store template lacks the microphone key [then] fail, [else stop].

    Same defect class on the sandboxed path: a sandbox without this key also
    refuses the microphone, and the template is the only place it can come from."""
    data = plistlib.loads((TAURI_DIR / "Entitlements.appstore.template.plist").read_bytes())
    assert data.get(MIC) is True


@pytest.mark.requirement("INSTALL-33")
def test_an_entitled_signature_passes(tmp_path: Path) -> None:
    """[if] an entitled signature is refused [then] fail, [else stop].

    Positive control: the gate can say yes, so its no below means something."""
    bin_dir, _ = _stub_toolchain(tmp_path, dump=_ENTITLED_DUMP)
    result = _sign("verify-app-entitlements", str(_app(tmp_path)), PATH=_path_with(bin_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"[OK] Open DJ.app carries {MIC}" in result.stdout


@pytest.mark.requirement("INSTALL-33")
@pytest.mark.parametrize(
    "dump",
    [
        _EMPTY_DUMP,
        # Substring trap: a longer key that merely contains the name.
        f"<plist><dict><key>{MIC}.extra</key><true/></dict></plist>",
        # The engine's JIT key alone: entitled, just not for the microphone.
        "<plist><dict><key>com.apple.security.cs.allow-unsigned-executable-memory</key><true/></dict></plist>",
    ],
    ids=["no-entitlements", "longer-key", "engine-key-only"],
)
def test_a_signature_without_the_microphone_is_refused(tmp_path: Path, dump: str) -> None:
    """[if] a signature without the microphone key passes [then] fail, [else stop]."""
    bin_dir, _ = _stub_toolchain(tmp_path, dump=dump)
    result = _sign("verify-app-entitlements", str(_app(tmp_path)), PATH=_path_with(bin_dir))
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert f"is signed without {MIC}" in combined
    assert "[OK]" not in combined


@pytest.mark.requirement("INSTALL-33")
def test_an_unreadable_signature_is_not_a_verdict(tmp_path: Path) -> None:
    """[if] a failing codesign is read as a verdict [then] fail, [else stop].

    codesign failing is UNKNOWN, never a pass, even if it printed the key."""
    bin_dir, _ = _stub_toolchain(tmp_path, dump=_ENTITLED_DUMP, codesign_exit=1)
    result = _sign("verify-app-entitlements", str(_app(tmp_path)), PATH=_path_with(bin_dir))
    assert result.returncode != 0
    assert "could not read the entitlements" in result.stdout + result.stderr


def _notarize_app(tmp_path: Path, dump: str) -> tuple[subprocess.CompletedProcess[str], str]:
    bin_dir, calls = _stub_toolchain(tmp_path, dump=dump)
    result = _sign(
        "notarize-app",
        str(_app(tmp_path)),
        PATH=_path_with(bin_dir),
        MDT_MACOS_SIGNING_IDENTITY=UNRESOLVABLE_IDENTITY,
        MDT_MACOS_NOTARY_KEYCHAIN_PROFILE="opendj-notary",
    )
    return result, calls.read_text() if calls.exists() else ""


@pytest.mark.requirement("INSTALL-33")
def test_notarize_app_refuses_an_unentitled_app_before_submitting(tmp_path: Path) -> None:
    """[if] notarize-app submits an unentitled app [then] fail, [else stop]."""
    result, calls = _notarize_app(tmp_path, _EMPTY_DUMP)
    assert result.returncode != 0
    assert f"is signed without {MIC}" in result.stdout + result.stderr
    assert "notarytool submit" not in calls


@pytest.mark.requirement("INSTALL-33")
def test_notarize_app_still_submits_an_entitled_app(tmp_path: Path) -> None:
    """[if] notarize-app refuses an entitled app [then] fail, [else stop].

    Overshoot control: the gate must not stop a correctly signed app."""
    result, calls = _notarize_app(tmp_path, _ENTITLED_DUMP)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "notarytool submit" in calls


@pytest.mark.requirement("INSTALL-33")
def test_verify_dmg_app_checks_the_entitlements_too() -> None:
    """[if] verify-dmg-app stops checking the entitlements [then] fail, [else stop].

    The dmg check mounts an image, so this pins the call rather than running it."""
    source = SIGN_SCRIPT.read_text()
    start = source.index("cmd_verify_dmg_app() {")
    body = source[start : source.index("\n}\n", start)]
    assert '_require_app_entitlements "$app"' in body
