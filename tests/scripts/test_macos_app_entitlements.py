"""INSTALL-33: the signed desktop app may use the microphone.

Hit live on demon-llama, Fri 2 Oct 2026, desktop app 0.1.5: clicking I/O raised
"headphone output acquisition failed: The request is not allowed by the user
agent or the platform in the current context" and macOS never showed its
microphone prompt. The app is signed with the hardened runtime and its
signature carried no entitlements at all, so macOS refused the microphone
before asking. Without the microphone WebKit withholds device names, so the
I/O lists cannot be named and cue latency calibration cannot listen.

The signature cases run the real signing script against a real app bundle
that real `codesign` signs ad hoc (no certificate needed), so they need macOS
and skip elsewhere, naming the codesign toolchain. The plist and config
checks are plain file reads and run on every platform.

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
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.scripts.test_macos_signing import SIGN_SCRIPT, UNRESOLVABLE_IDENTITY, _sign

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
TAURI_DIR: Path = REPO_ROOT / "apps/desktop/src-tauri"
APP_ENTITLEMENTS: Path = TAURI_DIR / "Entitlements.app.plist"
MIC = "com.apple.security.device.audio-input"
ENGINE_JIT = "com.apple.security.cs.allow-unsigned-executable-memory"

# Only macOS has codesign; Linux and Windows skip here, by name, rather than
# assert on a "codesign not found" message.
needs_codesign = pytest.mark.skipif(
    shutil.which("codesign") is None or shutil.which("true") is None,
    reason="needs the macOS codesign toolchain to sign a real app bundle",
)


def _app(tmp_path: Path, entitlements: dict[str, bool] | None) -> Path:
    """A real, minimal app bundle, ad hoc signed by real codesign.

    `entitlements=None` leaves it unsigned, so `codesign -d` genuinely fails."""
    app = tmp_path / "Open DJ.app"
    macos = app / "Contents/MacOS"
    macos.mkdir(parents=True)
    exe = macos / "opendj-desktop"
    # copyfile, not copy2: copy2 also copies the system file's restricted
    # flag, and macOS refuses that with "Operation not permitted".
    shutil.copyfile(shutil.which("true") or "", exe)
    os.chmod(exe, 0o755)
    (app / "Contents/Info.plist").write_bytes(
        plistlib.dumps({"CFBundleExecutable": exe.name, "CFBundleIdentifier": "dev.opendj.test-entitlements"})
    )
    if entitlements is None:
        return app
    cmd = ["codesign", "--force", "--sign", "-"]
    if entitlements:
        ents = tmp_path / "ents.plist"
        ents.write_bytes(plistlib.dumps(entitlements))
        cmd += ["--entitlements", str(ents)]
    subprocess.run([*cmd, str(app)], check=True, capture_output=True)
    return app


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
@needs_codesign
def test_an_entitled_signature_passes(tmp_path: Path) -> None:
    """[if] an entitled signature is refused [then] fail, [else stop].

    Positive control: the gate can say yes, so its no below means something."""
    result = _sign("verify-app-entitlements", str(_app(tmp_path, {MIC: True})))
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"[OK] Open DJ.app carries {MIC}" in result.stdout


@pytest.mark.requirement("INSTALL-33")
@needs_codesign
@pytest.mark.parametrize(
    "entitlements",
    [
        {},
        # Substring trap: a longer key that merely contains the name.
        {f"{MIC}.extra": True},
        # The engine's JIT key alone: entitled, just not for the microphone.
        {ENGINE_JIT: True},
    ],
    ids=["no-entitlements", "longer-key", "engine-key-only"],
)
def test_a_signature_without_the_microphone_is_refused(tmp_path: Path, entitlements: dict[str, bool]) -> None:
    """[if] a signature without the microphone key passes [then] fail, [else stop]."""
    result = _sign("verify-app-entitlements", str(_app(tmp_path, entitlements)))
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert f"is signed without {MIC}" in combined
    assert "[OK]" not in combined


@pytest.mark.requirement("INSTALL-33")
@needs_codesign
def test_an_unreadable_signature_is_not_a_verdict(tmp_path: Path) -> None:
    """[if] a failing codesign is read as a verdict [then] fail, [else stop].

    An unsigned bundle makes real `codesign -d` fail: that is UNKNOWN, never a pass."""
    result = _sign("verify-app-entitlements", str(_app(tmp_path, None)))
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "could not read the entitlements" in combined
    assert "[OK]" not in combined


@pytest.mark.requirement("INSTALL-33")
@needs_codesign
def test_notarize_app_refuses_an_unentitled_app_before_submitting(tmp_path: Path) -> None:
    """[if] notarize-app gets past an unentitled app [then] fail, [else stop].

    The refusal names the key, and nothing is handed to notarytool."""
    result = _sign(
        "notarize-app",
        str(_app(tmp_path, {})),
        MDT_MACOS_SIGNING_IDENTITY=UNRESOLVABLE_IDENTITY,
        MDT_MACOS_NOTARY_KEYCHAIN_PROFILE="opendj-test-no-such-profile",
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert f"is signed without {MIC}" in combined
    assert "notarytool" not in combined


@pytest.mark.requirement("INSTALL-33")
@needs_codesign
def test_notarize_app_lets_an_entitled_app_past_the_gate(tmp_path: Path) -> None:
    """[if] notarize-app refuses an entitled app at the entitlement gate [then] fail, [else stop].

    Overshoot control. The submission after the gate then fails on a notary
    profile that does not exist, which this test does not judge."""
    result = _sign(
        "notarize-app",
        str(_app(tmp_path, {MIC: True})),
        MDT_MACOS_SIGNING_IDENTITY=UNRESOLVABLE_IDENTITY,
        MDT_MACOS_NOTARY_KEYCHAIN_PROFILE="opendj-test-no-such-profile",
    )
    combined = result.stdout + result.stderr
    assert f"[OK] Open DJ.app carries {MIC}" in combined
    assert f"is signed without {MIC}" not in combined


@pytest.mark.requirement("INSTALL-33")
def test_verify_dmg_app_checks_the_entitlements_too() -> None:
    """[if] verify-dmg-app stops checking the entitlements [then] fail, [else stop].

    The dmg check mounts an image, so this pins the call rather than running it."""
    source = SIGN_SCRIPT.read_text()
    start = source.index("cmd_verify_dmg_app() {")
    body = source[start : source.index("\n}\n", start)]
    assert '_require_app_entitlements "$app"' in body
