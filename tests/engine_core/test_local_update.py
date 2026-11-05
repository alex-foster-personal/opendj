"""Restart to update: the pending-update record and its routes (INSTALL-39, INSTALL-40).

Real archives built in tmp_path (a gzip tar holding an ``.app`` with a payload
manifest), the real FastAPI routes, the real shell command broker. Nothing is
mocked; the shell's install is the only half not exercised here (Rust tests in
apps/desktop/src-tauri/src/local_update.rs cover the signature refusal).

Regression lines:
- if ready accepts an archive with no .sig then the nudge offers a build the
  shell will refuse
- if status shows pending for the running build then the nudge never goes away
- if install enqueues after the archive was rebuilt then a different build
  than the nudge named gets installed
- if two installs can be in flight then two installers race for one bundle
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.engine_core.build_info import BUILD_IDENTITY_STATE_ATTR, BuildIdentity, BuildInfoOut
from apps.engine_core.local_update import (
    ARTIFACT_PATH,
    CANCEL_PATH,
    INSTALL_COUNTDOWN_S,
    INSTALL_PATH,
    INSTALL_STATUS_PATH,
    MANIFEST_PATH,
    PENDING_FILE_NAME,
    READY_PATH,
    STATUS_PATH,
    ArtifactRefused,
    PendingStore,
    add_app_update_routes,
    inspect_artifact,
    resolve_status,
)
from apps.webui.server.routes.commands import router as commands_router
from tests.engine_core.local_update_archive import BUILT_SHA, SIGNATURE
from tests.engine_core.local_update_archive import build_archive as _archive

RUNNING_SHA = "0d41a28c0000000000000000000000000000beef"


def _app(tmp_path: Path, *, source: str = "payload", countdown_s: float = 0.0) -> FastAPI:
    app = FastAPI()
    identity = BuildIdentity(
        info=BuildInfoOut(
            source=source, engine_version="1.0.0-alpha.1", git_sha=RUNNING_SHA[:8], git_sha_full=RUNNING_SHA,
            git_branch="af--set-rc", git_dirty=False, built_at_utc="2026-11-02T22:59:45Z",
            built_at_kind="payload-build", app_version="1.0.0-alpha.1",
        ),
        failure=None,
    )
    setattr(app.state, BUILD_IDENTITY_STATE_ATTR, identity)
    add_app_update_routes(app, data_dir=tmp_path / "data", countdown_s=countdown_s)
    app.include_router(commands_router, prefix="/api/v1")
    return app


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1:51817")


# ----- the record -----------------------------------------------------------
@pytest.mark.requirement("INSTALL-39")
def test_ready_reads_the_build_stamp_from_inside_the_archive(tmp_path: Path) -> None:
    """[if] ready takes the build stamp from anywhere but inside the archive [then] the nudge can name the wrong build, [else stop]."""
    pending = inspect_artifact(_archive(tmp_path), "  set fix  ")
    assert pending.git_sha_full == BUILT_SHA
    assert pending.git_sha == BUILT_SHA[:8]
    assert pending.app_version == "1.0.0-alpha.1"
    assert pending.release_channel == "local-unnotarized"
    assert pending.signature == SIGNATURE
    assert pending.notes == "set fix"
    assert len(pending.sha256) == 64


@pytest.mark.requirement("INSTALL-39")
@pytest.mark.parametrize(
    ("build", "code"),
    [
        (lambda p: _archive(p, sign=False), "signature-missing"),
        (lambda p: _archive(p, stamp=False), "build-stamp-missing"),
        (lambda p: _archive(p, sha="not-a-sha"), "build-stamp-unreadable"),
        (lambda p: _archive(p, name="Open DJ.zip"), "artifact-not-updater-archive"),
        (lambda p: p / "absent.app.tar.gz", "artifact-missing"),
    ],
)
def test_ready_refuses_what_the_shell_could_not_install(tmp_path: Path, build, code: str) -> None:
    """[if] ready accepts an archive the shell would refuse [then] the nudge offers an uninstallable build, [else stop]."""
    with pytest.raises(ArtifactRefused) as refused:
        inspect_artifact(build(tmp_path), None)
    assert refused.value.code == code


@pytest.mark.requirement("INSTALL-39")
def test_a_zip_named_like_an_archive_is_refused(tmp_path: Path) -> None:
    """[if] a zip renamed to look like the archive is accepted [then] ready trusts a file name over its bytes, [else stop]."""
    fake = tmp_path / "Open DJ.app.tar.gz"
    fake.write_bytes(b"PK\x03\x04 not gzip")
    fake.with_name(fake.name + ".sig").write_text(SIGNATURE)
    with pytest.raises(ArtifactRefused) as refused:
        inspect_artifact(fake, None)
    assert refused.value.code == "artifact-not-gzip"


@pytest.mark.requirement("INSTALL-39")
def test_status_states_and_only_pending_is_news(tmp_path: Path) -> None:
    """[if] status reports news for any state but pending [then] the update nudge shows when nothing is waiting, [else stop]."""
    pending = inspect_artifact(_archive(tmp_path), None)
    assert resolve_status(None, RUNNING_SHA, "1").state == "none"
    assert resolve_status(pending, RUNNING_SHA, "1").state == "pending"
    assert resolve_status(pending, BUILT_SHA, "1").state == "installed"
    os.utime(pending.artifact, ns=(pending.mtime_ns + 10**9, pending.mtime_ns + 10**9))
    assert resolve_status(pending, RUNNING_SHA, "1").state == "artifact-changed"


@pytest.mark.requirement("INSTALL-39")
def test_the_record_survives_a_restart_and_a_corrupt_one_fails_loudly(tmp_path: Path) -> None:
    """[if] the pending record is lost on restart or a corrupt one reads as empty [then] updates vanish silently, [else stop]."""
    store = PendingStore(tmp_path)
    pending = inspect_artifact(_archive(tmp_path), None)
    store.write(pending)
    assert PendingStore(tmp_path).read() == pending
    (tmp_path / PENDING_FILE_NAME).write_text("{not json")
    with pytest.raises(RuntimeError, match="not a pending-update record"):
        store.read()


# ----- the routes -----------------------------------------------------------
@pytest.mark.requirement("INSTALL-39")
def test_ready_then_status_shows_the_pending_build(tmp_path: Path) -> None:
    """[if] status after ready does not show that pending build [then] the nudge never appears, [else stop]."""
    client = _client(_app(tmp_path))
    assert client.get(STATUS_PATH).json()["state"] == "none"
    ready = client.post(READY_PATH, json={"artifact": str(_archive(tmp_path)), "notes": "fix"})
    assert ready.status_code == 200, ready.text
    body = client.get(STATUS_PATH).json()
    assert body["state"] == "pending"
    assert body["pending"]["git_sha"] == BUILT_SHA[:8]
    assert "signature" not in body["pending"]


@pytest.mark.requirement("INSTALL-39")
def test_ready_refuses_the_running_build_and_bad_artifacts(tmp_path: Path) -> None:
    """[if] ready accepts the running build or a bad artifact [then] the nudge never goes away or offers junk, [else stop]."""
    client = _client(_app(tmp_path))
    same = client.post(READY_PATH, json={"artifact": str(_archive(tmp_path, sha=RUNNING_SHA))})
    assert same.status_code == 409
    assert same.json()["error"] == "already-running"
    (tmp_path / "unsigned").mkdir()
    unsigned = client.post(READY_PATH, json={"artifact": str(_archive(tmp_path / "unsigned", sign=False))})
    assert unsigned.status_code == 422
    assert unsigned.json()["error"] == "signature-missing"
    assert client.get(STATUS_PATH).json()["state"] == "none"


@pytest.mark.requirement("INSTALL-40")
def test_install_enqueues_one_shell_command_with_the_loopback_manifest(tmp_path: Path) -> None:
    """[if] install enqueues anything but one shell command carrying the loopback manifest [then] two installers can race, [else stop]."""
    client = _client(_app(tmp_path))
    client.post(READY_PATH, json={"artifact": str(_archive(tmp_path))})
    accepted = client.post(INSTALL_PATH)
    assert accepted.status_code == 202, accepted.text
    body = accepted.json()
    assert body["manifest_url"] == f"http://127.0.0.1:51817{MANIFEST_PATH}"
    assert body["git_sha_full"] == BUILT_SHA
    second = client.post(INSTALL_PATH)
    assert second.status_code == 409
    assert second.json()["error"] == "install-in-flight"
    claimed = client.get("/api/v1/commands/next", params={"consumer": "shell"}).json()
    assert claimed["command"] == {"type": "install-local-update", "manifest_url": body["manifest_url"], "git_sha_full": BUILT_SHA}
    status = client.get(INSTALL_STATUS_PATH.format(command_id=body["command_id"])).json()
    assert status["state"] == "claimed"


@pytest.mark.requirement("INSTALL-40")
def test_install_refuses_a_rebuilt_archive_and_a_dev_engine(tmp_path: Path) -> None:
    """[if] install proceeds on a rebuilt archive or a dev engine [then] a different build than the nudge named installs, [else stop]."""
    client = _client(_app(tmp_path))
    archive = _archive(tmp_path)
    client.post(READY_PATH, json={"artifact": str(archive)})
    stat = archive.stat()
    with archive.open("r+b") as handle:  # same size and mtime, different bytes
        handle.seek(stat.st_size - 1)
        handle.write(b"\x01")
    os.utime(archive, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    refused = client.post(INSTALL_PATH)
    assert refused.status_code == 409
    assert refused.json()["error"] == "artifact-changed"
    assert client.get("/api/v1/commands/next", params={"consumer": "shell"}).json() is None
    dev = _client(_app(tmp_path / "dev", source="repo"))
    assert dev.post(INSTALL_PATH).json()["error"] == "identity-unavailable"


@pytest.mark.requirement("INSTALL-40")
def test_manifest_and_artifact_feed_the_tauri_updater_over_loopback(tmp_path: Path) -> None:
    """[if] the manifest or artifact route does not serve the Tauri updater over loopback [then] the shell cannot fetch the update, [else stop]."""
    client = _client(_app(tmp_path))
    assert client.get(MANIFEST_PATH).status_code == 404
    archive = _archive(tmp_path)
    client.post(READY_PATH, json={"artifact": str(archive)})
    manifest = client.get(MANIFEST_PATH).json()
    (platform,) = manifest["platforms"].values()
    assert platform["signature"] == SIGNATURE
    assert platform["url"].startswith(f"http://127.0.0.1:51817{ARTIFACT_PATH}?sha256=")
    assert BUILT_SHA in manifest["notes"]
    assert client.get(ARTIFACT_PATH).content == archive.read_bytes()
    remote = TestClient(client.app, base_url="http://192.168.1.20:51817")
    assert remote.get(MANIFEST_PATH).status_code == 403
    assert remote.get(ARTIFACT_PATH).status_code == 403


# ----- the install countdown (INSTALL-43) -------------------------------------
def _next_shell_command(client: TestClient) -> object:
    return client.get("/api/v1/commands/next", params={"consumer": "shell"}).json()


@pytest.mark.requirement("INSTALL-43")
def test_the_installed_app_counts_down_ten_seconds() -> None:
    """[if] the shipped countdown is not 10 s [then] the maintainer's rule is broken, [else stop]."""
    assert INSTALL_COUNTDOWN_S == 10.0


@pytest.mark.requirement("INSTALL-43")
def test_install_is_held_back_from_the_shell_until_the_countdown_ends(tmp_path: Path) -> None:
    """[if] the shell can claim during the countdown [then] Esc Space x4 cannot cancel, [else stop]."""
    client = _client(_app(tmp_path, countdown_s=0.6))
    client.post(READY_PATH, json={"artifact": str(_archive(tmp_path))})
    accepted = client.post(INSTALL_PATH).json()
    assert accepted["countdown_s"] == 0.6
    assert accepted["countdown_ends_at_utc"].endswith("Z")
    assert _next_shell_command(client) is None
    install = client.get(STATUS_PATH).json()["install"]
    assert install["command_id"] == accepted["command_id"]
    assert install["state"] == "pending"
    assert 0 < install["countdown_remaining_s"] <= 0.6
    time.sleep(0.7)
    claimed = _next_shell_command(client)
    assert isinstance(claimed, dict) and claimed["command"]["git_sha_full"] == BUILT_SHA
    assert client.get(STATUS_PATH).json()["install"]["state"] == "claimed"


@pytest.mark.requirement("INSTALL-43")
def test_cancel_during_the_countdown_withdraws_the_order(tmp_path: Path) -> None:
    """[if] cancel lands in the countdown [then] the shell never receives the order, [else stop]."""
    client = _client(_app(tmp_path, countdown_s=0.4))
    client.post(READY_PATH, json={"artifact": str(_archive(tmp_path))})
    accepted = client.post(INSTALL_PATH).json()
    cancelled = client.post(CANCEL_PATH, json={"reason": "Esc Space x4"})
    assert cancelled.status_code == 200, cancelled.text
    body = cancelled.json()
    assert (body["cancelled"], body["command_id"], body["git_sha_full"]) == (True, accepted["command_id"], BUILT_SHA)
    assert body["install"]["state"] == "failed"
    assert body["install"]["outcome"] == "cancelled"
    assert "Esc Space x4" in body["install"]["error"]
    time.sleep(0.5)
    assert _next_shell_command(client) is None
    status = client.get(STATUS_PATH).json()
    assert status["state"] == "pending"  # still flagged: the nudge comes back
    assert status["install"]["outcome"] == "cancelled"
    again = client.post(CANCEL_PATH)
    assert again.status_code == 409
    assert again.json()["error"] == "not-counting-down"
    assert client.post(INSTALL_PATH).status_code == 202  # a cancelled order frees the slot


@pytest.mark.requirement("INSTALL-43")
def test_cancel_after_the_shell_claimed_is_refused(tmp_path: Path) -> None:
    """[if] cancel arrives once installing began [then] it is refused, never half-applied, [else stop]."""
    client = _client(_app(tmp_path, countdown_s=0.0))
    client.post(READY_PATH, json={"artifact": str(_archive(tmp_path))})
    client.post(INSTALL_PATH)
    assert isinstance(_next_shell_command(client), dict)
    refused = client.post(CANCEL_PATH)
    assert refused.status_code == 409
    assert refused.json()["error"] == "not-counting-down"
    assert "claimed" in refused.json()["detail"]


# ----- descent from the running build (INSTALL-44) ----------------------------
def _lineage(previous: str, *, descends: bool, missing: int = 0, line: str | None = None) -> dict[str, object]:
    return {
        "color_name": "white", "color_hex": "#ffffff", "lines": ["main"], "line_shares": {},
        "unresolved_lines": [], "previous_git_sha_full": previous, "descends_from_previous": descends,
        "missing_commits": missing, "missing_from": line,
    }


@pytest.mark.requirement("INSTALL-44")
def test_a_build_that_drops_the_running_builds_commits_warns_loudly(tmp_path: Path) -> None:
    """[if] the flagged build does not contain the running one [then] status and install say NOT DESCENDED, [else stop]."""
    client = _client(_app(tmp_path))
    archive = _archive(tmp_path, lineage=_lineage(RUNNING_SHA, descends=False, missing=328, line="af--set-rc"))
    ready = client.post(READY_PATH, json={"artifact": str(archive)}).json()
    descent = ready["pending"]["descent"]
    assert descent["verdict"] == "not-descended"
    assert descent["warning"] == "NOT DESCENDED FROM YOUR LAST BUILD: missing 328 commits from af--set-rc"
    assert ready["pending"]["lineage"]["color_name"] == "white"
    accepted = client.post(INSTALL_PATH)
    assert accepted.status_code == 202  # loud, never blocking: the maintainer's builds install
    assert accepted.json()["warning"] == descent["warning"]


@pytest.mark.requirement("INSTALL-44")
def test_descent_is_only_asserted_against_the_build_actually_running(tmp_path: Path) -> None:
    """[if] descent is judged against any build but the running one [then] NOT DESCENDED warns wrongly, [else stop]."""
    client = _client(_app(tmp_path))
    for sub in ("a", "b", "c"):
        (tmp_path / sub).mkdir()
    kept = _archive(tmp_path / "a", lineage=_lineage(RUNNING_SHA, descends=True))
    assert client.post(READY_PATH, json={"artifact": str(kept)}).json()["pending"]["descent"] == {
        "verdict": "descended", "running_git_sha_full": RUNNING_SHA, "previous_git_sha_full": RUNNING_SHA,
        "missing_commits": 0, "missing_from": None, "warning": None,
    }
    other = _archive(tmp_path / "b", lineage=_lineage("1" * 40, descends=True))
    unverified = client.post(READY_PATH, json={"artifact": str(other)}).json()["pending"]["descent"]
    assert unverified["verdict"] == "unverified"
    assert "unverified" in unverified["warning"]
    old = _archive(tmp_path / "c")
    assert client.post(READY_PATH, json={"artifact": str(old)}).json()["pending"]["descent"]["verdict"] == "unknown"
