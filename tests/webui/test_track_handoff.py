"""Open in Rekordbox / Open in djay hand-off (HANDOFF-01..03).

- [if] the app is installed and the file exists [then] the OS is asked to
  open that file in that app, and nothing of ours is written, [else stop].
- [if] the app is absent [then] the route answers 422 app_not_installed with
  a reason, and launches nothing, [else stop].
- [if] the track has no local file [then] 422 names why, [else stop].
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.webui.server import handoff_apps
from apps.webui.server.backend import InMemoryBackend
from tests.webui.app_fixtures import client_fixture  # noqa: F401  (pytest fixtures)


def _set_file_path(backend: InMemoryBackend, stable_id: str, file_path: str | None) -> None:
    backend.get_track(stable_id).file_path = file_path


def _mac_apps(root: Path, *bundles: str) -> Path:
    for bundle in bundles:
        (root / bundle).mkdir(parents=True)
    return root


def test_resolve_picks_the_newest_rekordbox_on_mac(tmp_path: Path) -> None:
    apps = _mac_apps(tmp_path, "rekordbox 6/rekordbox.app", "rekordbox 7/rekordbox.app")
    target = handoff_apps.resolve_target("rekordbox", system="Darwin", mac_app_dirs=[apps])
    assert target.available
    assert target.path == apps / "rekordbox 7" / "rekordbox.app"


def test_resolve_finds_djay_on_mac(tmp_path: Path) -> None:
    apps = _mac_apps(tmp_path, "djay Pro AI.app")
    target = handoff_apps.resolve_target("djay", system="Darwin", mac_app_dirs=[apps])
    assert target.path == apps / "djay Pro AI.app"


def test_absent_app_is_unavailable_with_a_reason(tmp_path: Path) -> None:
    target = handoff_apps.resolve_target("djay", system="Darwin", mac_app_dirs=[tmp_path])
    assert not target.available
    assert target.reason == "djay Pro is not installed in /Applications on this Mac"


def test_linux_reports_both_apps_unavailable() -> None:
    for app in handoff_apps.HANDOFF_APPS:
        target = handoff_apps.resolve_target(app, system="Linux")
        assert not target.available
        assert target.reason and "does not run on Linux" in target.reason


def test_windows_finds_rekordbox_in_either_install_layout(tmp_path: Path) -> None:
    """Pioneer's documented ``Pioneer\\rekordbox <v>`` layout and the older
    ``rekordbox\\rekordbox <v>`` one are both searched; the newest wins."""
    for exe in ("rekordbox/rekordbox 6.8.5/rekordbox.exe", "Pioneer/rekordbox 7.1.2/rekordbox.exe"):
        (tmp_path / exe).parent.mkdir(parents=True)
        (tmp_path / exe).write_bytes(b"")
    target = handoff_apps.resolve_target("rekordbox", system="Windows", program_files=str(tmp_path))
    assert target.path == tmp_path / "Pioneer" / "rekordbox 7.1.2" / "rekordbox.exe"


def test_windows_rekordbox_absent_from_both_layouts_says_so(tmp_path: Path) -> None:
    target = handoff_apps.resolve_target("rekordbox", system="Windows", program_files=str(tmp_path))
    assert not target.available
    assert "Program Files" in (target.reason or "")


def test_windows_djay_is_unavailable_with_its_reason() -> None:
    target = handoff_apps.resolve_target("djay", system="Windows")
    assert not target.available
    assert "no file-open path" in (target.reason or "")


def _stub_open(tmp_path: Path) -> tuple[Path, Path]:
    """A disposable opener program that records its argv, one call per line.

    Handed to ``open_in_app`` as its resolved macOS opener, so the real launch
    path runs end to end (exec, exit status); only the program is ours.
    """
    log = tmp_path / "open-argv.txt"
    stub = tmp_path / "bin" / "open"
    stub.parent.mkdir()
    stub.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"with open({str(log)!r}, 'a', encoding='utf-8') as fh:\n"
        "    fh.write('\\x00'.join(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return stub, log


_NEEDS_SHEBANG = pytest.mark.skipif(
    sys.platform == "win32", reason="the stub `open` is a shebang script, which Windows cannot exec"
)


@_NEEDS_SHEBANG
def test_open_in_mac_app_runs_open_dash_a(tmp_path: Path) -> None:
    apps = _mac_apps(tmp_path / "Applications", "rekordbox 7/rekordbox.app")
    audio = tmp_path / "track.flac"
    audio.write_bytes(b"fixture-audio")
    stub, log = _stub_open(tmp_path)
    handoff_apps.open_in_app("rekordbox", str(audio), system="Darwin", mac_app_dirs=[apps], mac_opener=stub)
    calls = [line.split("\x00") for line in log.read_text(encoding="utf-8").splitlines()]
    assert calls == [["-a", str(apps / "rekordbox 7" / "rekordbox.app"), str(audio)]]
    assert audio.read_bytes() == b"fixture-audio"


@_NEEDS_SHEBANG
def test_open_in_absent_app_launches_nothing(tmp_path: Path) -> None:
    audio = tmp_path / "track.flac"
    audio.write_bytes(b"x")
    stub, log = _stub_open(tmp_path)
    with pytest.raises(handoff_apps.HandoffError) as exc:
        handoff_apps.open_in_app(
            "djay", str(audio), system="Darwin", mac_app_dirs=[tmp_path / "Applications"], mac_opener=stub
        )
    assert exc.value.code == "app_not_installed"
    assert not log.exists()


def test_route_unknown_track_404(client: TestClient) -> None:
    r = client.post("/api/v1/tracks/missing-track-id:open-in?app=rekordbox")
    assert r.status_code == 404


def test_route_rejects_an_unknown_app(client: TestClient) -> None:
    r = client.post("/api/v1/tracks/track-001:open-in?app=traktor")
    assert r.status_code == 422


def test_route_streaming_track_422(client: TestClient, seed_backend: InMemoryBackend) -> None:
    _set_file_path(seed_backend, "track-001", "spotify:track:abc")
    r = client.post("/api/v1/tracks/track-001:open-in?app=djay")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "not_a_local_file"


def test_route_missing_file_422(client: TestClient, seed_backend: InMemoryBackend, tmp_path: Path) -> None:
    _set_file_path(seed_backend, "track-001", str(tmp_path / "gone.flac"))
    r = client.post("/api/v1/tracks/track-001:open-in?app=rekordbox")
    assert r.json()["detail"]["code"] == "file_missing"


def test_a_missing_mac_opener_is_named_not_searched_for(tmp_path: Path) -> None:
    """The opener is a resolved path: when it is absent the hand-off says so
    rather than falling back to whatever ``open`` PATH finds."""
    apps = _mac_apps(tmp_path / "Applications", "djay Pro.app")
    audio = tmp_path / "track.flac"
    audio.write_bytes(b"x")
    with pytest.raises(handoff_apps.HandoffError) as exc:
        handoff_apps.open_in_app(
            "djay", str(audio), system="Darwin", mac_app_dirs=[apps], mac_opener=tmp_path / "no-opener"
        )
    assert exc.value.code == "handoff_unavailable"
    assert str(tmp_path / "no-opener") in exc.value.message


@pytest.mark.skipif(
    sys.platform == "darwin",
    reason="on macOS the real route would launch an installed rekordbox; this checks the no-app answer",
)
def test_route_with_no_installed_app_says_so(client: TestClient, seed_backend: InMemoryBackend, tmp_path: Path) -> None:
    """The real route and resolver, no seam: off macOS, djay has no file-open
    path, so a local file gets 422 ``app_not_installed`` with its reason."""
    audio = tmp_path / "target.flac"
    audio.write_bytes(b"fixture-audio")
    _set_file_path(seed_backend, "track-001", str(audio))
    r = client.post("/api/v1/tracks/track-001:open-in?app=djay")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "app_not_installed"
    assert "djay" in detail["message"]
    assert audio.read_bytes() == b"fixture-audio"


def test_apps_route_lists_both_apps_with_reasons(client: TestClient) -> None:
    r = client.get("/api/v1/handoff/apps")
    assert r.status_code == 200
    apps = {row["app"]: row for row in r.json()["apps"]}
    assert set(apps) == {"rekordbox", "djay"}
    for row in apps.values():
        assert row["available"] or row["reason"], row
