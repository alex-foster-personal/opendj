"""Build identity: what the artifact says it is.

Single-line acceptance checks, in the repo's "if X then broken" shape:

- if a payload manifest is present and the endpoint still reports source=repo,
  an installed app describes the build machine's checkout instead of itself
  -> broken.
- if a missing or unreadable manifest renders as a blank or a guess instead of
  a 503 naming the path, a stale build looks identical to a good one
  -> broken.
- if git_dirty is dropped or defaulted, a build made from uncommitted work
  ships looking reproducible -> broken.
- if the resolver is re-run per request, every readout costs three git
  subprocesses and can disagree with the code that is running -> broken.
- if /api/v1/build-info is registered after the SPA mount, the Mount at "/"
  swallows it and the UI readout 404s -> broken.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.engine_core.build_info import (
    BUILD_INFO_PATH,
    MANIFEST_ENV,
    BuildInfoUnavailable,
    add_build_info_route,
    head_time_as_utc,
    resolve_build_info,
)

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

GOOD_IDENTITY: dict[str, object] = {
    "app_version": "0.1.0",
    "built_at_utc": "2026-08-19T12:00:00Z",
    "bundle_identifier": "com.opendj.desktop.lane-b",
    "engine_version": "0.1.0",
    "git_branch": "af--engine-sidecar",
    "git_dirty": True,
    "git_sha": "0d41a28c",
    "git_sha_full": "0d41a28c0000000000000000000000000000beef",
    "lane_label": "B",
    "product_name": "Open DJ (B)",
}


def _manifest(tmp_path: Path, identity: dict[str, object] | None) -> Path:
    body: dict[str, object] = {"schema": 1, "kind": "opendj-engine-payload"}
    if identity is not None:
        body["identity"] = identity
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _client(environ: dict[str, str], repo_root: Path = REPO_ROOT) -> Iterator[TestClient]:
    app = FastAPI()
    add_build_info_route(app, environ=environ, repo_root=repo_root)
    with TestClient(app) as client:
        yield client


# ----- payload source ----------------------------------------------------
@pytest.mark.requirement("INSTALL-07")
def test_manifest_is_served_as_the_payload_identity(tmp_path: Path) -> None:
    path = _manifest(tmp_path, GOOD_IDENTITY)
    info = resolve_build_info({MANIFEST_ENV: str(path)}, REPO_ROOT)
    assert info.source == "payload"
    assert info.git_sha == "0d41a28c"
    assert info.lane_label == "B"
    assert info.product_name == "Open DJ (B)"
    assert info.built_at_kind == "payload-build"
    assert info.manifest_path == str(path)


@pytest.mark.requirement("INSTALL-07")
def test_dirty_flag_survives_verbatim(tmp_path: Path) -> None:
    """A dirty build must not be able to present itself as a clean one."""
    dirty = _manifest(tmp_path / "d", {**GOOD_IDENTITY, "git_dirty": True})
    clean = _manifest(tmp_path / "c", {**GOOD_IDENTITY, "git_dirty": False})
    assert resolve_build_info({MANIFEST_ENV: str(dirty)}, REPO_ROOT).git_dirty is True
    assert resolve_build_info({MANIFEST_ENV: str(clean)}, REPO_ROOT).git_dirty is False


# ----- fault paths -------------------------------------------------------
@pytest.mark.requirement("INSTALL-07")
def test_missing_manifest_is_a_named_fault(tmp_path: Path) -> None:
    absent = tmp_path / "nowhere/manifest.json"
    with pytest.raises(BuildInfoUnavailable) as excinfo:
        resolve_build_info({MANIFEST_ENV: str(absent)}, REPO_ROOT)
    assert str(absent) in str(excinfo.value)


@pytest.mark.requirement("INSTALL-07")
def test_unparseable_manifest_is_a_named_fault(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(BuildInfoUnavailable) as excinfo:
        resolve_build_info({MANIFEST_ENV: str(path)}, REPO_ROOT)
    assert str(path) in str(excinfo.value)


@pytest.mark.requirement("INSTALL-07")
def test_manifest_without_identity_is_refused(tmp_path: Path) -> None:
    path = _manifest(tmp_path, None)
    with pytest.raises(BuildInfoUnavailable):
        resolve_build_info({MANIFEST_ENV: str(path)}, REPO_ROOT)


@pytest.mark.requirement("INSTALL-07")
def test_partial_identity_is_refused_rather_than_half_rendered(tmp_path: Path) -> None:
    partial = {key: value for key, value in GOOD_IDENTITY.items() if key != "git_dirty"}
    path = _manifest(tmp_path, partial)
    with pytest.raises(BuildInfoUnavailable) as excinfo:
        resolve_build_info({MANIFEST_ENV: str(path)}, REPO_ROOT)
    assert "git_dirty" in str(excinfo.value)


# ----- repo source -------------------------------------------------------
@pytest.mark.requirement("INSTALL-07")
def test_repo_checkout_describes_itself_from_live_git() -> None:
    info = resolve_build_info({}, REPO_ROOT)
    assert info.source == "repo"
    assert info.built_at_kind == "engine-start"
    assert len(info.git_sha_full) == 40
    assert info.git_sha == info.git_sha_full[:8]
    assert info.lane_label is None


@pytest.mark.requirement("INSTALL-28")
def test_repo_built_at_is_engine_start_instant() -> None:
    """[if] repo engine restarts [then] built_at_utc is the resolution instant."""
    info = resolve_build_info({}, REPO_ROOT)
    built = datetime.fromisoformat(info.built_at_utc.replace("Z", "+00:00"))
    assert datetime.now(UTC) - built < timedelta(minutes=2)


@pytest.mark.requirement("INSTALL-07")
def test_repo_checkout_carries_the_tauri_app_version() -> None:
    expected = json.loads(
        (REPO_ROOT / "apps/desktop/src-tauri/tauri.conf.json").read_text(
            encoding="utf-8"
        )
    )["version"]
    info = resolve_build_info({}, REPO_ROOT)
    assert info.app_version == expected


@pytest.mark.requirement("INSTALL-07")
def test_a_directory_git_cannot_describe_is_a_fault(tmp_path: Path) -> None:
    with pytest.raises(BuildInfoUnavailable):
        resolve_build_info({}, tmp_path)


# ----- route -------------------------------------------------------------
@pytest.mark.requirement("INSTALL-07")
def test_route_serves_the_payload_identity(tmp_path: Path) -> None:
    path = _manifest(tmp_path, GOOD_IDENTITY)
    for client in _client({MANIFEST_ENV: str(path)}):
        response = client.get(BUILD_INFO_PATH)
        assert response.status_code == 200
        body = response.json()
        assert body["source"] == "payload"
        assert body["bundle_identifier"] == "com.opendj.desktop.lane-b"
        assert body["git_dirty"] is True


@pytest.mark.requirement("INSTALL-07")
def test_route_reports_503_with_the_reason_when_identity_is_unavailable(
    tmp_path: Path,
) -> None:
    absent = tmp_path / "gone.json"
    for client in _client({MANIFEST_ENV: str(absent)}):
        response = client.get(BUILD_INFO_PATH)
        assert response.status_code == 503
        body = response.json()
        assert body["error"] == "build_identity_unavailable"
        assert str(absent) in body["message"]


@pytest.mark.requirement("INSTALL-07")
def test_identity_is_resolved_once_not_per_request(tmp_path: Path) -> None:
    """Deleting the manifest after boot must not change the answer.

    The running process is still the build the manifest described, so a
    readout that changed mid-life would be reporting the filesystem, not the
    code.
    """
    path = _manifest(tmp_path, GOOD_IDENTITY)
    for client in _client({MANIFEST_ENV: str(path)}):
        assert client.get(BUILD_INFO_PATH).status_code == 200
        path.unlink()
        second = client.get(BUILD_INFO_PATH)
        assert second.status_code == 200
        assert second.json()["git_sha"] == "0d41a28c"


# ----- wiring ------------------------------------------------------------
@pytest.mark.requirement("INSTALL-07")
def test_engine_registers_build_info_ahead_of_the_spa_mount() -> None:
    """A Mount at "/" matches everything, so ordering is the whole contract."""
    source = (REPO_ROOT / "apps/engine_core/app.py").read_text(encoding="utf-8")
    assert source.index("add_build_info_route(") < source.index("_mount_spa(app)")
    assert source.index("add_host_info_route(") < source.index("_mount_spa(app)")
    assert source.index("add_perf_tier_route(") < source.index("_mount_spa(app)")


# ----- the repo timestamp is really UTC ----------------------------------
@pytest.mark.requirement("INSTALL-07")
def test_head_time_is_converted_to_utc() -> None:
    """git's %cI carries the COMMITTER's offset, not UTC.

    Shipping "2026-08-19T14:17:23+01:00" under a field called built_at_utc is
    the nearly-right readout this endpoint exists to replace: it looks like an
    answer and disagrees by an hour with the payload path, which stamps real
    UTC.
    """
    assert head_time_as_utc("2026-08-19T14:17:23+01:00") == "2026-08-19T13:17:23Z"
    assert head_time_as_utc("2026-08-19T13:17:23Z") == "2026-08-19T13:17:23Z"
    assert head_time_as_utc("2026-08-19T00:30:00-05:00") == "2026-08-19T05:30:00Z"


@pytest.mark.requirement("INSTALL-07")
def test_a_timestamp_without_a_zone_is_refused_not_assumed() -> None:
    with pytest.raises(BuildInfoUnavailable, match="no timezone"):
        head_time_as_utc("2026-08-19T14:17:23")


@pytest.mark.requirement("INSTALL-07")
def test_an_unparseable_timestamp_is_refused_not_guessed() -> None:
    with pytest.raises(BuildInfoUnavailable, match=r"not.*ISO-8601"):
        head_time_as_utc("last tuesday")


@pytest.mark.requirement("INSTALL-07")
def test_the_repo_path_serves_a_z_suffixed_timestamp() -> None:
    """End to end against this very checkout, not a fixture."""
    info = resolve_build_info({}, REPO_ROOT)
    assert info.source == "repo"
    assert info.built_at_utc.endswith("Z")
    assert "+" not in info.built_at_utc
