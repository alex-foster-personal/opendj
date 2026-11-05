"""One app version for every surface (public-launch sweep N12, Wed 7 Oct 2026).

The public 1.0.0-alpha.1 build reported 0.1.0 from the engine's health and
hello, and from Settings > Backend, because each carried a literal of its own.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.shared import app_version as av

REPO = Path(__file__).resolve().parents[2]
APP_VERSION_IN_TREE = json.loads((REPO / av.APP_PACKAGE_REL).read_text(encoding="utf-8"))["version"]


def _repo_with(tmp_path: Path, conf: object) -> Path:
    path = tmp_path / av.APP_PACKAGE_REL
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(conf), encoding="utf-8")
    return tmp_path


def _manifest(tmp_path: Path, identity: object) -> Path:
    path = tmp_path / "payload-manifest.json"
    path.write_text(json.dumps({"identity": identity}), encoding="utf-8")
    return path


def test_a_checkout_reports_the_app_package_version() -> None:
    assert av.resolve_app_version({}, REPO) == APP_VERSION_IN_TREE
    assert av.APP_VERSION == APP_VERSION_IN_TREE


def test_control_another_conf_version_is_what_is_reported(tmp_path: Path) -> None:
    root = _repo_with(tmp_path, {"version": "9.9.9-beta.3"})
    assert av.resolve_app_version({}, root) == "9.9.9-beta.3"


def test_a_packaged_engine_reports_its_manifest_not_the_tree(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, {"app_version": "1.0.0-alpha.2"})
    env = {av.MANIFEST_ENV: str(manifest)}
    assert av.resolve_app_version(env, REPO) == "1.0.0-alpha.2"


@pytest.mark.parametrize("identity", [{}, {"app_version": ""}, {"app_version": 1}, "not-an-object"])
def test_a_manifest_without_a_version_is_unknown_not_the_tree(tmp_path: Path, identity: object) -> None:
    manifest = _manifest(tmp_path, identity)
    env = {av.MANIFEST_ENV: str(manifest)}
    assert av.resolve_app_version(env, REPO) == av.UNKNOWN_VERSION


def test_a_named_manifest_that_is_missing_is_unknown(tmp_path: Path) -> None:
    env = {av.MANIFEST_ENV: str(tmp_path / "absent.json")}
    assert av.resolve_app_version(env, REPO) == av.UNKNOWN_VERSION


@pytest.mark.parametrize("conf", [{}, {"version": "  "}, ["1.0.0"]])
def test_a_checkout_without_a_version_is_unknown(tmp_path: Path, conf: object) -> None:
    assert av.resolve_app_version({}, _repo_with(tmp_path, conf)) == av.UNKNOWN_VERSION


def test_version_files_that_are_not_utf8_are_unknown(tmp_path: Path) -> None:
    root = _repo_with(tmp_path, {"version": "1.0.0"})
    (root / av.APP_PACKAGE_REL).write_bytes(b'{"version": "1.0.0\xff"}')
    assert av.resolve_app_version({}, root) == av.UNKNOWN_VERSION
    manifest = tmp_path / "payload-manifest.json"
    manifest.write_bytes(b'{"identity": {"app_version": "\xff"}}')
    assert av.resolve_app_version({av.MANIFEST_ENV: str(manifest)}, REPO) == av.UNKNOWN_VERSION


def test_engine_and_backend_report_the_app_version() -> None:
    from apps import webui
    from apps.engine_core.config import ENGINE_VERSION
    from apps.webui.server.app import OPENAPI_DOCUMENT_VERSION, create_app

    assert ENGINE_VERSION == APP_VERSION_IN_TREE
    assert webui.__version__ == APP_VERSION_IN_TREE
    app = create_app(mount_frontend=False)
    assert app.state.version == APP_VERSION_IN_TREE
    # The OpenAPI document keeps its own contract version, so a release bump
    # never rewrites the committed apps/webui/openapi.json.
    assert app.openapi()["info"]["version"] == OPENAPI_DOCUMENT_VERSION
