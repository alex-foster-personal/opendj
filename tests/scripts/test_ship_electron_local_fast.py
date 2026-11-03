"""ship_dmg.sh --shell electron --local-fast: a Developer ID signed Electron app for this Mac only (OPS-63).

[if] --local-fast is combined with --unsigned, or names a --machine that is not this Mac [then] exit 2 before anything runs, [else stop].
[if] --local-fast --dry-run runs [then] the plan names payload signing, the manifest refresh, the app signing and both signature checks, [else stop].
[if] the packed app nests Mach-O code [then] it is signed inside out, payload excluded, the app last, [else stop].
[if] a signed app lacks the hardened runtime, the Developer ID authority or an entitlement [then] verification raises, [else stop].
[if] a signed app's Info.plist has no NSMicrophoneUsageDescription [then] verification raises (row 45), [else stop].

The synthetic bundles below are SYNTHESIZED fixtures: directories laid out like
an Electron app whose Mach-O files are four magic bytes, enough for the order
logic, which reads only the layout and the magic. The real-signing test signs a
copy of the Electron.app that pnpm installed with this Mac's real Developer ID
identity, and runs only where both exist (a macOS build host).

Regression lines:
  - if --local-fast installs on any Mac but this one then broken (an
    un-notarized app must never be copied elsewhere)
  - if a framework or helper is signed before the Mach-O inside it then broken
    (the outer signature seals a child that is about to change)
  - if the payload is re-signed by the app pass then broken (it loses the
    engine entitlements and every numba JIT is killed)
  - if a signed build without the microphone usage text verifies then broken
    (macOS never prompts, cue calibration and set recording hear silence)

-Claude
"""

from __future__ import annotations

import json
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from scripts import electron_local_sign, ship_electron
from scripts.electron_local_sign import LocalSignError, SignTarget

pytestmark = pytest.mark.requirement("OPS-63")

REPO = Path(__file__).resolve().parents[2]
ENTITLEMENTS = REPO / electron_local_sign.ELECTRON_ENTITLEMENTS_REL
IDENTITY = "Developer ID Application: Test Owner (TEAMID1234)"
MACHO = bytes.fromhex("cffaedfe") + b"\0" * 28


def _done(stdout: str = "", stderr: str = "", code: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, stdout, stderr)


def _write(path: Path, data: bytes = MACHO) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _synthetic_app(root: Path) -> Path:
    """SYNTHESIZED: the Electron 44 layout, Mach-O files reduced to their magic."""
    app = root / "Open DJ.app"
    c = app / "Contents"
    _write(c / "MacOS" / "Open DJ")
    _write(c / "Info.plist", b"<plist/>")
    fw = c / "Frameworks" / "Electron Framework.framework" / "Versions" / "A"
    _write(fw / "Electron Framework")
    _write(fw / "Libraries" / "libffmpeg.dylib")
    _write(fw / "Helpers" / "chrome_crashpad_handler")
    _write(fw / "Resources" / "en.lproj" / "locale.pak", b"PAK!")
    (c / "Frameworks" / "Electron Framework.framework" / "Electron Framework").symlink_to(
        "Versions/A/Electron Framework"
    )
    _write(c / "Frameworks" / "Squirrel.framework" / "Versions" / "A" / "Squirrel")
    _write(c / "Frameworks" / "Squirrel.framework" / "Versions" / "A" / "Resources" / "ShipIt")
    for helper in ("Open DJ Helper", "Open DJ Helper (Renderer)"):
        _write(c / "Frameworks" / f"{helper}.app" / "Contents" / "MacOS" / helper)
    _write(c / "Resources" / "payload" / "runtime" / "bin" / "python3.12")
    _write(c / "Resources" / "payload" / "bin" / "odj-audio")
    _write(c / "Resources" / "app.asar", b"asar")
    return app


# ----- argument refusals ------------------------------------------------------------


def _opts(**overrides: object) -> ship_electron.Options:
    base: dict = dict(
        dry_run=True, unsigned=False, sha="HEAD", machines=["silver"], no_close=False, local="silver", local_fast=True
    )
    return ship_electron.Options(**{**base, **overrides})


def test_local_fast_refuses_unsigned() -> None:
    assert "cannot be combined with --unsigned" in str(ship_electron.local_fast_refusal(_opts(unsigned=True)))


@pytest.mark.parametrize("machines", [["air"], ["silver", "air"], ["air", "silver", "demon-llama"]])
def test_local_fast_refuses_any_other_mac(machines: list[str]) -> None:
    refusal = ship_electron.local_fast_refusal(_opts(machines=machines))
    assert refusal is not None and "--machine silver" in refusal


def test_local_fast_accepts_exactly_this_mac() -> None:
    assert ship_electron.local_fast_refusal(_opts()) is None
    assert ship_electron.local_fast_refusal(_opts(local_fast=False, machines=["air"])) is None


def test_default_machines_are_all_three_so_local_fast_without_machine_exits_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = {"MDT_LOCAL_NAME": "silver", "PATH": ""}
    assert ship_electron.main(["--local-fast", "--dry-run"], env) == ship_electron.EXIT_USAGE
    assert "installs on this Mac only" in capsys.readouterr().err


def test_local_fast_with_unsigned_env_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    env = {"MDT_LOCAL_NAME": "silver", "MDT_SHIP_UNSIGNED": "1", "PATH": ""}
    assert ship_electron.main(["--local-fast", "--machine", "silver", "--dry-run"], env) == ship_electron.EXIT_USAGE
    assert "MDT_SHIP_UNSIGNED" in capsys.readouterr().err


def test_without_unsigned_or_local_fast_the_refusal_names_local_fast(capsys: pytest.CaptureFixture[str]) -> None:
    assert ship_electron.main(["--dry-run"], {"MDT_LOCAL_NAME": "silver"}) == ship_electron.EXIT_SIGNING_UNIMPLEMENTED
    err = capsys.readouterr().err
    assert "signing needs the owner's Apple credentials" in err and "--local-fast" in err


def test_local_fast_dry_run_plans_every_signing_step_and_runs_none(capsys: pytest.CaptureFixture[str]) -> None:
    env = {"MDT_LOCAL_NAME": "silver", electron_local_sign.IDENTITY_ENV: IDENTITY}
    assert ship_electron.main(["--local-fast", "--machine", "silver", "--dry-run"], env) == 0
    out = capsys.readouterr().out
    assert "Developer ID signed, NOT notarized (local-fast)" in out
    titles = [line.split(". ", 1)[1] for line in out.splitlines() if line.startswith("[PLAN] ") and ". " in line[:12]]
    order = [
        "build the engine payload",
        "sign the staged payload",
        "pack the shell",
        "replace the placeholder payload",
        "refresh the payload manifest",
        "verify the stamps",
        "install on silver",
        "prove the installed signature on silver",
    ]
    positions = [next(i for i, t in enumerate(titles) if t.startswith(want)) for want in order]
    assert positions == sorted(positions), titles
    assert f"bash scripts/sign_macos_developer_id.sh payload {ship_electron.PAYLOAD_STAGE_REL}" in out
    assert "python -m scripts.payload_signed_manifest --payload" in out
    assert f"--entitlements {electron_local_sign.ELECTRON_ENTITLEMENTS_REL}" in out
    assert "ssh " not in out, "a local-fast plan reaches no other Mac"
    assert "[PLAN] not done: notarization," in out
    assert "[DRY-RUN] nothing was built, deleted or installed" in out


# ----- identity ------------------------------------------------------------


def test_identity_env_wins_and_runs_nothing() -> None:
    def run(argv: Sequence[str]) -> subprocess.CompletedProcess:
        raise AssertionError(f"ran {argv}")

    assert electron_local_sign.resolve_signing_identity({electron_local_sign.IDENTITY_ENV: IDENTITY}, run) == IDENTITY


def _keychain(listing: str):
    def run(argv: Sequence[str]) -> subprocess.CompletedProcess:
        assert list(argv[:2]) == ["security", "find-identity"], argv
        return _done(listing)

    return run


def test_identity_falls_back_to_the_single_keychain_identity(tmp_path: Path) -> None:
    listing = f'  1) {"A" * 40} "{IDENTITY}"\n     1 valid identities found\n'
    assert electron_local_sign.resolve_signing_identity({"HOME": str(tmp_path)}, _keychain(listing)) == IDENTITY


@pytest.mark.parametrize(
    "listing",
    [
        "     0 valid identities found\n",
        f'  1) {"A" * 40} "{IDENTITY}"\n  2) {"B" * 40} "Developer ID Application: Other (OTHER12345)"\n',
        f'  1) {"A" * 40} "Apple Development: Someone (DEV1234567)"\n',
    ],
)
def test_identity_refuses_none_or_several(tmp_path: Path, listing: str) -> None:
    with pytest.raises(LocalSignError, match="no single Developer ID identity"):
        electron_local_sign.resolve_signing_identity({"HOME": str(tmp_path)}, _keychain(listing))


# ----- order ------------------------------------------------------------


def test_nested_code_is_signed_inside_out_payload_excluded_app_last(tmp_path: Path) -> None:
    app = _synthetic_app(tmp_path)
    order = electron_local_sign.nested_code_order(app)
    rel = [str(t.path.relative_to(app)) for t in order]
    frameworks = "Contents/Frameworks"
    assert rel[-1] == "." and order[-1].entitlements, "the app is signed last, with entitlements"
    assert not any("Resources/payload" in r for r in rel), "the payload keeps its engine-entitlement signatures"
    assert not any(r.endswith(("locale.pak", "app.asar", "Info.plist")) for r in rel), "only Mach-O is signed"
    for child, parent in [
        (
            f"{frameworks}/Electron Framework.framework/Versions/A/Libraries/libffmpeg.dylib",
            "Electron Framework.framework",
        ),
        (
            f"{frameworks}/Electron Framework.framework/Versions/A/Helpers/chrome_crashpad_handler",
            "Electron Framework.framework",
        ),
        (f"{frameworks}/Squirrel.framework/Versions/A/Resources/ShipIt", "Squirrel.framework"),
    ]:
        assert rel.index(child) < rel.index(f"{frameworks}/{parent}")
    helpers = [t for t in order if t.path.suffix == ".app" and t.path != app]
    assert len(helpers) == 2 and all(t.entitlements for t in helpers)
    assert all(not t.entitlements for t in order if t.path.suffix != ".app")
    assert f"{frameworks}/Electron Framework.framework/Versions/A/Electron Framework" not in rel, (
        "a framework's main binary is signed with its framework"
    )


def test_codesign_command_is_hardened_and_entitlements_only_where_asked(tmp_path: Path) -> None:
    plain = electron_local_sign.codesign_command(SignTarget(tmp_path / "x.dylib", False), IDENTITY, ENTITLEMENTS)
    entitled = electron_local_sign.codesign_command(SignTarget(tmp_path / "A.app", True), IDENTITY, ENTITLEMENTS)
    assert plain[:7] == ["codesign", "--force", "--options", "runtime", "--timestamp=none", "--sign", IDENTITY]
    assert "--entitlements" not in plain
    assert entitled[entitled.index("--entitlements") + 1] == str(ENTITLEMENTS)


def test_the_shipped_entitlements_grant_every_required_key() -> None:
    declared = plistlib.loads(ENTITLEMENTS.read_bytes())
    assert {k for k in electron_local_sign.ELECTRON_ENTITLEMENTS if declared.get(k) is True} == set(
        electron_local_sign.ELECTRON_ENTITLEMENTS
    )
    electron_local_sign.check_entitlements_file(ENTITLEMENTS)


def test_a_codesign_failure_stops_the_signing(tmp_path: Path) -> None:
    app = _synthetic_app(tmp_path)
    with pytest.raises(LocalSignError, match="codesign failed on"):
        electron_local_sign.sign_app(app, IDENTITY, ENTITLEMENTS, lambda argv: _done(stderr="boom", code=1))


# ----- verification ------------------------------------------------------------

GOOD_DV = f"Identifier=com.opendj.desktop\nCodeDirectory v=20500 size=1 flags=0x10000(runtime) hashes=1\nAuthority={IDENTITY}\n"
GOOD_ENT = "".join(f"<key>{k}</key><true/>" for k in electron_local_sign.ELECTRON_ENTITLEMENTS)


def _verifier(dv: str = GOOD_DV, ent: str = GOOD_ENT, verify_code: int = 0):
    def run(argv: Sequence[str]) -> subprocess.CompletedProcess:
        if "--verify" in argv:
            return _done(stderr="valid on disk", code=verify_code)
        if "-dv" in argv:
            return _done(stderr=dv)
        return _done(stdout=ent)

    return run


MIC_TEXT = "Open DJ records your set from the input you pick."


def _app_with_info(root: Path, info: dict | None = None) -> Path:
    """SYNTHESIZED: an app folder holding only Contents/Info.plist (codesign is faked)."""
    app = root / "Open DJ.app"
    plist = {electron_local_sign.MICROPHONE_USAGE_KEY: MIC_TEXT} if info is None else info
    _write(app / "Contents" / "Info.plist", plistlib.dumps(plist))
    return app


def test_verification_accepts_a_hardened_developer_id_app(tmp_path: Path) -> None:
    lines = electron_local_sign.verify_signed_app(_app_with_info(tmp_path), _verifier())
    assert any(IDENTITY in line for line in lines)
    assert any("NSMicrophoneUsageDescription present" in line for line in lines)


@pytest.mark.parametrize(
    ("verifier", "why"),
    [
        (_verifier(verify_code=1), "--strict failed"),
        (_verifier(dv=GOOD_DV.replace("flags=0x10000(runtime)", "flags=0x0(none)")), "hardened runtime"),
        (_verifier(dv=GOOD_DV.replace(IDENTITY, "Apple Development: Someone (DEV1234567)")), "Developer ID"),
        (_verifier(ent=GOOD_ENT.replace("device.audio-input", "device.camera")), "audio-input"),
    ],
)
def test_verification_refuses_what_is_missing(tmp_path: Path, verifier, why: str) -> None:
    with pytest.raises(LocalSignError, match=why):
        electron_local_sign.verify_signed_app(_app_with_info(tmp_path), verifier)


@pytest.mark.requirement("ELECTRON-8399")
@pytest.mark.parametrize("info", [{}, {"NSMicrophoneUsageDescription": "  "}, {"NSMicrophoneUsageDescription": 1}])
def test_verification_refuses_a_signed_app_without_the_microphone_prompt_text(tmp_path: Path, info: dict) -> None:
    with pytest.raises(LocalSignError, match="NSMicrophoneUsageDescription"):
        electron_local_sign.verify_signed_app(_app_with_info(tmp_path, info), _verifier())


@pytest.mark.requirement("ELECTRON-8399")
def test_verification_refuses_an_app_with_no_readable_info_plist(tmp_path: Path) -> None:
    with pytest.raises(LocalSignError, match="unreadable"):
        electron_local_sign.verify_signed_app(tmp_path / "Open DJ.app", _verifier())


@pytest.mark.requirement("ELECTRON-8399")
def test_both_shells_declare_the_microphone_prompt_and_the_audio_input_entitlement() -> None:
    """Row 45: the Electron pack config carries what the Tauri bundle carries."""
    electron_pkg = json.loads((REPO / "apps/desktop/electron/package.json").read_text())
    electron_text = electron_pkg["build"]["mac"]["extendInfo"][electron_local_sign.MICROPHONE_USAGE_KEY]
    tauri_info = plistlib.loads((REPO / "apps/desktop/src-tauri/Info.plist").read_bytes())
    assert electron_text.strip() and tauri_info[electron_local_sign.MICROPHONE_USAGE_KEY].strip()
    audio_input = "com.apple.security.device.audio-input"
    assert plistlib.loads(ENTITLEMENTS.read_bytes()).get(audio_input) is True
    tauri_app_ent = plistlib.loads((REPO / "apps/desktop/src-tauri/Entitlements.app.plist").read_bytes())
    assert tauri_app_ent.get(audio_input) is True


# ----- the real thing ------------------------------------------------------------


def _installed_electron_app() -> Path | None:
    found = sorted(
        (REPO / "apps/desktop/electron/node_modules/.pnpm").glob("electron@*/node_modules/electron/dist/Electron.app")
    )
    return found[-1] if found else None


def _real_identity() -> str | None:
    try:
        return electron_local_sign.resolve_signing_identity({"HOME": str(Path.home())})
    except LocalSignError:
        return None


@pytest.mark.skipif(
    sys.platform != "darwin", reason="codesign and a Developer ID identity exist only on a macOS build host"
)
def test_real_developer_id_signing_of_a_real_electron_bundle(tmp_path: Path) -> None:
    """Signs a COPY of pnpm's Electron.app with this Mac's real identity; launches nothing."""
    source, identity = _installed_electron_app(), _real_identity()
    if source is None or identity is None:
        pytest.skip(f"UNAVAILABLE: electron dist {source}, Developer ID identity {identity}")
    app = tmp_path / "Open DJ.app"
    subprocess.run([electron_local_sign.resolve_tool("ditto"), str(source), str(app)], check=True)
    payload_bin = app / "Contents" / "Resources" / "payload" / "bin" / "odj-tool"
    payload_bin.parent.mkdir(parents=True)
    shutil.copy2(app / "Contents/Frameworks/Squirrel.framework/Versions/A/Resources/ShipIt", payload_bin)
    before = subprocess.run(
        [electron_local_sign.resolve_tool("codesign"), "-dv", str(payload_bin)],
        capture_output=True,
        text=True,
        check=False,
    ).stderr
    info_path = app / "Contents" / "Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    info[electron_local_sign.MICROPHONE_USAGE_KEY] = MIC_TEXT  # pack:dir adds it from build.mac.extendInfo
    info_path.write_bytes(plistlib.dumps(info))
    signed = electron_local_sign.sign_app(app, identity, ENTITLEMENTS)
    assert signed >= 10
    lines = electron_local_sign.verify_signed_app(app)
    assert any("hardened runtime" in line for line in lines)
    after = subprocess.run(
        [electron_local_sign.resolve_tool("codesign"), "-dv", str(payload_bin)],
        capture_output=True,
        text=True,
        check=False,
    ).stderr
    assert after == before, "the app pass must not re-sign anything under Contents/Resources/payload"


def test_a_missing_signing_tool_stops_naming_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A real empty PATH: the default runner resolves codesign before launching it."""
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(LocalSignError, match="codesign is not on PATH"):
        electron_local_sign.verify_signed_app(tmp_path / "Open DJ.app")


# ----- acceptance ---------------------------------------------------------------------
# - if --local-fast on demon-llama schedules the installed-app acceptance then
#   the quick rebuild loop runs the full analysis and stems checks -> broken


def test_local_fast_on_demon_llama_schedules_no_acceptance() -> None:
    opts = _opts(machines=["demon-llama"], local="demon-llama")
    steps = ship_electron.post_install_steps(
        opts, "0" * 40, "run", ship_electron.Runner("demon-llama"), ship_electron.post.Outcome()
    )
    titles = [step.title for step in steps]
    assert not any(t.startswith("installed-app acceptance on demon-llama") for t in titles), titles
    assert any("SKIPPED for --local-fast" in t for t in titles), titles


def test_control_a_plain_build_on_demon_llama_still_schedules_acceptance() -> None:
    opts = _opts(machines=["demon-llama"], local="demon-llama", local_fast=False, unsigned=True)
    steps = ship_electron.post_install_steps(
        opts, "0" * 40, "run", ship_electron.Runner("demon-llama"), ship_electron.post.Outcome()
    )
    titles = [step.title for step in steps]
    assert any(t.startswith("installed-app acceptance on demon-llama") for t in titles), titles
