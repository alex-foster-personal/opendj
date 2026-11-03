"""scripts/electron_release.py: the notarized Electron release, its feeds and the Tauri bridge (ELECTRON-09).

[if] a non-proof version differs from package.json or tauri.conf.json [then] it is refused naming all three, [else stop].
[if] latest-mac.yml names other bytes, another size or another version than the zip on disk [then] the check raises, [else stop].
[if] spctl does not say accepted with source=Notarized Developer ID [then] verification raises, [else stop].
[if] the bridge signature is empty [then] building latest.json raises, [else stop].
[if] a release signs nested code [then] every codesign call carries a secure timestamp, [else stop].

Regression lines:
  - if latest-mac.yml carries an absolute or spaced url then broken (GitHub rewrites spaces; the feed 404s)
  - if a release is built with --timestamp=none then broken (the notary service refuses it)
  - if the bridge latest.json points anywhere but the immutable app-v<version> tag then broken

-Claude
"""

from __future__ import annotations

import base64
import hashlib
import json
import plistlib
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from scripts import electron_local_sign, electron_release
from scripts.electron_local_sign import SignTarget
from scripts.electron_release import Artifacts, ReleaseError

pytestmark = pytest.mark.requirement("ELECTRON-09")

SHA = "0123456789abcdef0123456789abcdef01234567"
WHEN = datetime(2026, 11, 3, 4, 5, 6, tzinfo=UTC)


def _zip(tmp_path: Path, version: str = "1.0.0-alpha.2") -> Path:
    path = Artifacts(tmp_path, version).zip
    path.write_bytes(b"PK\x03\x04 synthetic zip bytes")
    return path


def test_release_version_must_be_the_single_version_source() -> None:
    assert (
        electron_release.resolve_release_version(None, "1.0.0-alpha.2", "1.0.0-alpha.2", proof=False) == "1.0.0-alpha.2"
    )
    with pytest.raises(
        ReleaseError, match=r"package\.json \(1\.0\.0-alpha\.2\).*tauri\.conf\.json \(1\.0\.0-alpha\.1\)"
    ):
        electron_release.resolve_release_version(None, "1.0.0-alpha.2", "1.0.0-alpha.1", proof=False)
    with pytest.raises(ReleaseError):
        electron_release.resolve_release_version("1.0.0-alpha.3", "1.0.0-alpha.2", "1.0.0-alpha.2", proof=False)


def test_proof_version_may_differ_but_must_be_semver() -> None:
    assert (
        electron_release.resolve_release_version("1.0.0-alpha.90", "1.0.0-alpha.1", "1.0.0-alpha.1", proof=True)
        == "1.0.0-alpha.90"
    )
    with pytest.raises(ValueError):
        electron_release.resolve_release_version("not-a-version", "1.0.0-alpha.1", "1.0.0-alpha.1", proof=True)


def test_artifact_names_carry_no_space(tmp_path: Path) -> None:
    art = Artifacts(tmp_path, "1.0.0-alpha.2")
    names = [art.dmg.name, art.zip.name, art.archive.name, art.signature.name, art.feed.name, art.bridge.name]
    assert all(" " not in name for name in names), names
    assert art.archive.name.endswith(".app.tar.gz")
    assert art.feed.name == "latest-mac.yml" and art.bridge.name == "latest.json"


def test_latest_mac_yml_names_the_zip_bytes(tmp_path: Path) -> None:
    zip_path = _zip(tmp_path)
    text = electron_release.latest_mac_yml("1.0.0-alpha.2", zip_path, SHA, WHEN)
    feed = yaml.safe_load(text)
    expected = base64.b64encode(hashlib.sha512(zip_path.read_bytes()).digest()).decode()
    assert feed["version"] == "1.0.0-alpha.2"
    assert feed["files"] == [{"url": zip_path.name, "sha512": expected, "size": zip_path.stat().st_size}]
    assert feed["path"] == zip_path.name and feed["sha512"] == expected
    assert feed["releaseDate"] == "2026-11-03T04:05:06.000Z"
    assert feed["releaseNotes"] == f"built from {SHA}"
    electron_release.check_feed_matches(text, zip_path, "1.0.0-alpha.2")


def test_feed_check_refuses_other_bytes_size_or_version(tmp_path: Path) -> None:
    zip_path = _zip(tmp_path)
    text = electron_release.latest_mac_yml("1.0.0-alpha.2", zip_path, SHA, WHEN)
    with pytest.raises(ReleaseError, match="version"):
        electron_release.check_feed_matches(text, zip_path, "1.0.0-alpha.3")
    zip_path.write_bytes(b"PK\x03\x04 other bytes, same zip name")
    with pytest.raises(ReleaseError, match="sha512"):
        electron_release.check_feed_matches(text, zip_path, "1.0.0-alpha.2")
    feed = yaml.safe_load(electron_release.latest_mac_yml("1.0.0-alpha.2", zip_path, SHA, WHEN))
    feed["files"][0]["size"] += 1
    with pytest.raises(ReleaseError, match="size"):
        electron_release.check_feed_matches(yaml.safe_dump(feed), zip_path, "1.0.0-alpha.2")


def test_bridge_manifest_points_at_the_immutable_tag() -> None:
    manifest = electron_release.bridge_manifest(
        "1.0.0-alpha.2", SHA, "OpenDJ-1.0.0-alpha.2-aarch64.app.tar.gz", "sig\n", "nightly", WHEN
    )
    entry = manifest["platforms"]["darwin-aarch64"]
    assert entry == {
        "signature": "sig",
        "url": "https://github.com/alex-foster-personal/issue-assets/releases/download/app-v1.0.0-alpha.2/OpenDJ-1.0.0-alpha.2-aarch64.app.tar.gz",
    }
    assert manifest["version"] == "1.0.0-alpha.2" and SHA in manifest["notes"]
    assert manifest["pub_date"] == "2026-11-03T04:05:06Z" and manifest["channel"] == "nightly"
    with pytest.raises(ReleaseError, match="empty"):
        electron_release.bridge_manifest("1.0.0-alpha.2", SHA, "a.app.tar.gz", "  \n", "nightly", WHEN)


def test_spctl_must_say_notarized_developer_id(tmp_path: Path) -> None:
    app = tmp_path / "Open DJ.app"
    good = f"{app}: accepted\nsource=Notarized Developer ID\norigin=Developer ID Application: X (5K6FYJMYR6)\n"
    electron_release.require_spctl_accepted(good, app)
    for bad in (f"{app}: rejected\nsource=Unnotarized Developer ID\n", f"{app}: accepted\nsource=Developer ID\n"):
        with pytest.raises(ReleaseError, match="did not accept"):
            electron_release.require_spctl_accepted(bad, app)


def test_notarization_ids_are_read_from_notarytool_output() -> None:
    out = (
        "Conducting pre-submission checks...\n  id: 2efe2717-52ef-43a5-96dc-0797e4ca1041\n"
        "Processing complete\n  id: 2efe2717-52ef-43a5-96dc-0797e4ca1041\n  status: Accepted\n"
    )
    assert electron_release.notarization_ids(out) == ["2efe2717-52ef-43a5-96dc-0797e4ca1041"]


def test_proof_rewrites_only_the_payload_app_version(tmp_path: Path) -> None:
    manifest = {"identity": {"app_version": "1.0.0-alpha.1", "git_sha_full": SHA}, "bytes": {"total": 1}}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    electron_release.set_payload_app_version(tmp_path, "1.0.0-alpha.90")
    after = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert after == {"identity": {"app_version": "1.0.0-alpha.90", "git_sha_full": SHA}, "bytes": {"total": 1}}
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ReleaseError, match="app_version"):
        electron_release.set_payload_app_version(tmp_path, "1.0.0-alpha.90")


def test_release_signing_carries_a_secure_timestamp(tmp_path: Path) -> None:
    entitlements = Path(electron_release.REPO_ROOT) / electron_local_sign.ELECTRON_ENTITLEMENTS_REL
    target = SignTarget(tmp_path / "A.app", True)
    release = electron_local_sign.codesign_command(
        target, "Developer ID Application: T (TEAMID1234)", entitlements, timestamp=True
    )
    local = electron_local_sign.codesign_command(target, "Developer ID Application: T (TEAMID1234)", entitlements)
    assert "--timestamp" in release and "--timestamp=none" not in release
    assert "--timestamp=none" in local


def test_verify_refuses_an_untimestamped_release_signature(tmp_path: Path) -> None:
    described = "flags=0x10000(runtime)\nAuthority=Developer ID Application: T (TEAMID1234)\n"
    granted = " ".join(electron_local_sign.ELECTRON_ENTITLEMENTS)

    def fake(argv: list[str]) -> subprocess.CompletedProcess:
        if "--entitlements" in argv:
            return subprocess.CompletedProcess(argv, 0, granted, "")
        return subprocess.CompletedProcess(argv, 0, "", described)

    info = tmp_path / "Contents" / "Info.plist"
    info.parent.mkdir(parents=True)
    info.write_bytes(plistlib.dumps({electron_local_sign.MICROPHONE_USAGE_KEY: "records your set"}))
    electron_local_sign.verify_signed_app(tmp_path, fake)
    with pytest.raises(electron_local_sign.LocalSignError, match="secure timestamp"):
        electron_local_sign.verify_signed_app(tmp_path, fake, require_timestamp=True)


def test_preflight_refuses_unsigned_or_missing_profile(tmp_path: Path) -> None:
    opts = electron_release.Options(out=tmp_path / "out", version="1.0.0-alpha.2", proof=False, channel="nightly")
    for env, needle in (
        ({"MDT_SHIP_UNSIGNED": "1"}, "MDT_SHIP_UNSIGNED"),
        ({"TAURI_SIGNING_PRIVATE_KEY": "k"}, "MDT_MACOS_NOTARY_KEYCHAIN_PROFILE"),
        ({"MDT_MACOS_NOTARY_KEYCHAIN_PROFILE": "p"}, "TAURI_SIGNING_PRIVATE_KEY"),
    ):
        release = electron_release.Release(opts, env, SHA)
        if electron_release.platform.system() != "Darwin" or electron_release.platform.machine() != "arm64":
            with pytest.raises(ReleaseError, match="arm64 Mac"):
                release.preflight()
            continue
        with pytest.raises(ReleaseError, match=needle):
            release.preflight()
    assert not (tmp_path / "out").exists()


def test_proof_env_becomes_ls_environment_and_only_for_proofs() -> None:
    flags = electron_release.proof_environment_args(
        ["HOME=/tmp/p/home", "OPENDJ_SHELL_DATA_DIR=/tmp/p/data"], proof=True
    )
    assert flags == [
        "-c.mac.extendInfo.LSEnvironment.HOME=/tmp/p/home",
        "-c.mac.extendInfo.LSEnvironment.OPENDJ_SHELL_DATA_DIR=/tmp/p/data",
    ]
    assert electron_release.proof_environment_args([], proof=False) == []
    with pytest.raises(ReleaseError, match="without --proof"):
        electron_release.proof_environment_args(["HOME=/x"], proof=False)
    for bad in ("HOME", "lower=x", "=x"):
        with pytest.raises(ReleaseError, match="KEY=VALUE"):
            electron_release.proof_environment_args([bad], proof=True)


def test_main_refuses_proof_env_on_a_release_before_building(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert electron_release.main(["--out", str(tmp_path / "out"), "--proof-env", "HOME=/x"], env={}) == 1
    assert "without --proof" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_versions_must_be_bare_semver() -> None:
    for bad in ("v1.0.0-alpha.2", " 1.0.0-alpha.2", "1.0.0-alpha.2\n"):
        with pytest.raises(ReleaseError, match="bare semver"):
            electron_release.resolve_release_version(bad, "1.0.0-alpha.1", "1.0.0-alpha.1", proof=True)


def test_app_update_config_names_the_shell_feed(tmp_path: Path) -> None:
    updater_ts = (electron_release.REPO_ROOT / "apps/desktop/electron/src/updater.ts").read_text(encoding="utf-8")
    assert f"export const UPDATE_FEED_URL = '{electron_release.UPDATE_FEED_URL}';" in updater_ts
    app = tmp_path / "Open DJ.app"
    (app / "Contents" / "Resources").mkdir(parents=True)
    electron_release.write_app_update_config(app)
    written = yaml.safe_load((app / "Contents/Resources/app-update.yml").read_text(encoding="utf-8"))
    assert written == {
        "provider": "generic",
        "url": electron_release.UPDATE_FEED_URL,
        "updaterCacheDirName": "opendj-desktop-electron-updater",
    }
