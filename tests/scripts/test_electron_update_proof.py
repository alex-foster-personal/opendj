"""scripts/electron_update_proof.py: the private N -> N+1 Electron update proof (ELECTRON-08).

[if] --root is the home itself, outside the home, inside Applications, or already exists [then] it is refused, [else stop].
[if] the N build's LSEnvironment does not point inside --root and at the local feed [then] the run refuses before launching, [else stop].
[if] the marker row written before the update is read back after it [then] library continuity holds, [else stop].
[if] a lock file has no integer port [then] it is not treated as an engine, [else stop].

Regression lines:
  - if a proof launch can reach the machine's real data dir then broken (the relaunched app would open the real library)
  - if cleanup can signal a process outside --root then broken

-Claude
"""

from __future__ import annotations

import json
import plistlib
import socket
from pathlib import Path

import pytest

from scripts import electron_update_proof as proof
from scripts.electron_update_proof import Layout, ProofError

pytestmark = pytest.mark.requirement("ELECTRON-08")


def _app(tmp_path: Path, environment: dict[str, str] | None, version: str = "1.0.0-alpha.1.800") -> Path:
    app = tmp_path / "N" / "Open DJ.app"
    (app / "Contents").mkdir(parents=True)
    info: dict[str, object] = {"CFBundleShortVersionString": version}
    if environment is not None:
        info["LSEnvironment"] = environment
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    return app


def test_root_must_be_a_fresh_folder_under_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "taken").mkdir(parents=True)
    proof.check_root(home / "odj-proof-1", home)
    for bad, needle in (
        (home, "new folder under"),
        (tmp_path / "elsewhere", "new folder under"),
        (home / "Applications" / "x", "Applications"),
        (home / "taken", "already exists"),
    ):
        with pytest.raises(ProofError, match=needle):
            proof.check_root(bad, home)


def test_isolation_requires_the_baked_environment_to_name_this_run(tmp_path: Path) -> None:
    layout = Layout(tmp_path / "root")
    good = proof.expected_environment(layout, 8765)
    assert good == {
        "HOME": str(layout.home),
        "CFFIXED_USER_HOME": str(layout.home),
        "OPENDJ_UPDATE_FEED_URL": "http://127.0.0.1:8765",
    }
    proof.check_isolation(_app(tmp_path / "a", good), layout, 8765)
    with pytest.raises(ProofError, match="does not isolate"):
        proof.check_isolation(_app(tmp_path / "b", None), layout, 8765)
    with pytest.raises(ProofError, match="OPENDJ_UPDATE_FEED_URL"):
        proof.check_isolation(_app(tmp_path / "c", good), layout, 9999)
    escaped = {k: v for k, v in good.items() if k != "CFFIXED_USER_HOME"}
    with pytest.raises(ProofError, match="CFFIXED_USER_HOME"):
        proof.check_isolation(_app(tmp_path / "d", escaped), layout, 8765)


def test_bundle_version_reads_the_info_plist(tmp_path: Path) -> None:
    assert proof.bundle_version(_app(tmp_path, None, "1.0.0-alpha.1.900")) == "1.0.0-alpha.1.900"


def test_marker_survives_and_keeps_the_inode(tmp_path: Path) -> None:
    state_db = tmp_path / "state.db"
    inode = proof.write_marker(state_db, "proof-1")
    assert proof.read_markers(state_db) == ["proof-1"]
    assert state_db.stat().st_ino == inode


def test_a_lock_without_an_integer_port_is_not_an_engine(tmp_path: Path) -> None:
    lock = tmp_path / ".engine.lock"
    assert proof.read_lock(lock) is None
    lock.write_text(json.dumps({"pid": 1}), encoding="utf-8")
    assert proof.read_lock(lock) is None
    lock.write_text("not json", encoding="utf-8")
    assert proof.read_lock(lock) is None
    lock.write_text(json.dumps({"pid": 7, "port": 51234}), encoding="utf-8")
    assert proof.read_lock(lock) == {"pid": 7, "port": 51234}


def test_cleanup_only_matches_processes_naming_the_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "odj-proof"
    listing = (
        f"  11 {root}/apps/Open DJ.app/Contents/MacOS/Open DJ\n"
        "  12 /Applications/Open DJ.app/Contents/MacOS/opendj-desktop\n"
        f"  13 opendj-engine --name opendj-engine [{root}/apps/Open DJ.app/Contents/Resources/payload/runtime/bin/python3]\n"
        f"  14 caffeinate -dimsu python3 electron_update_proof.py --root {root} --port 8765\n"
    )

    class Done:
        stdout = listing

    monkeypatch.setattr(proof.subprocess, "run", lambda *a, **k: Done())
    # 14 is this run's own ancestor (it names the root only in its arguments) and is excluded by the caller.
    assert proof.pids_under(root, exclude={14}) == [11, 13]


def test_main_refuses_a_bad_root_and_writes_evidence(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    code = proof.main(
        [
            "--n-app", str(tmp_path / "N.app"),
            "--feed", str(tmp_path / "feed"),
            "--expect-version", "1.0.0-alpha.1.900",
            "--root", "/odj-update-proof-outside-home",
            "--port", "8765",
            "--evidence", str(evidence),
        ]
    )  # fmt: skip
    assert code == 1
    assert "new folder under" in json.loads(evidence.read_text(encoding="utf-8"))["failure"]


def test_both_shells_share_the_data_dir_under_the_isolated_home(tmp_path: Path) -> None:
    expected = tmp_path / "root" / "home" / "Library" / "Application Support" / "com.opendj.desktop"
    assert Layout(tmp_path / "root").data == expected


def test_a_bridge_layout_uses_the_library_free_account_home(tmp_path: Path) -> None:
    layout = Layout(tmp_path / "acct/proof-1", bridge=True, account_home=tmp_path / "acct")
    assert layout.data == tmp_path / "acct/Library/Application Support/com.opendj.desktop"
    assert proof.expected_environment(layout, 8766)["HOME"] == str(tmp_path / "acct")
    with pytest.raises(ProofError, match="account home"):
        _ = Layout(tmp_path / "root", bridge=True).home


def test_bridge_cleanup_removes_every_app_trace_and_nothing_else(tmp_path: Path) -> None:
    lib = tmp_path / "Library"
    for rel in (
        "Application Support/com.opendj.desktop/state", "Caches/com.opendj.desktop.ShipIt",
        "WebKit/com.opendj.desktop", "Application Support/Open DJ", "Application Support/Keep",
    ):  # fmt: skip
        (lib / rel).mkdir(parents=True)
    (lib / "Preferences").mkdir()
    (lib / "Preferences/com.opendj.desktop.plist").write_bytes(b"")
    removed = proof.remove_app_traces(tmp_path)
    assert len(removed) == 5 and proof.app_traces(tmp_path) == []
    assert (lib / "Application Support/Keep").is_dir()


def test_bridge_manifest_is_pointed_at_the_local_feed(tmp_path: Path) -> None:
    archive = tmp_path / "OpenDJ-1.0.0-alpha.1.950-aarch64.app.tar.gz"
    archive.write_bytes(b"\x1f\x8b")
    url = (
        f"https://github.com/alex-foster-personal/issue-assets/releases/download/app-v1.0.0-alpha.1.950/{archive.name}"
    )
    manifest = {"version": "1.0.0-alpha.1.950", "platforms": {"darwin-aarch64": {"signature": "s", "url": url}}}
    (tmp_path / "latest.json").write_text(json.dumps(manifest), encoding="utf-8")
    proof.point_bridge_at_local_feed(tmp_path / "latest.json", 8766)
    entry = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))["platforms"]["darwin-aarch64"]
    assert entry == {"signature": "s", "url": f"http://127.0.0.1:8766/{archive.name}"}
    archive.unlink()
    with pytest.raises(ProofError, match="not in the feed"):
        proof.point_bridge_at_local_feed(tmp_path / "latest.json", 8766)


def test_a_squatted_feed_port_is_refused_by_name() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as squatter:
        squatter.bind(("127.0.0.1", 0))
        squatter.listen(1)
        port = squatter.getsockname()[1]
        with pytest.raises(ProofError, match="already in use"):
            proof.require_port_free(port)


def test_a_refused_existing_root_is_never_deleted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    existing = home / "precious"
    (existing / "keep.txt").parent.mkdir(parents=True)
    (existing / "keep.txt").write_text("mine", encoding="utf-8")
    monkeypatch.setattr(proof.Path, "home", staticmethod(lambda: home))
    evidence = tmp_path / "evidence.json"
    code = proof.main(
        [
            "--n-app", str(tmp_path / "N.app"), "--feed", str(tmp_path / "feed"),
            "--expect-version", "1.0.0-alpha.1.900", "--root", str(existing), "--port", "8765",
            "--evidence", str(evidence),
        ]
    )  # fmt: skip
    assert code == 1 and (existing / "keep.txt").read_text(encoding="utf-8") == "mine"
    assert "already exists" in json.loads(evidence.read_text(encoding="utf-8"))["failure"]


def test_ancestors_are_never_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    class Done:
        stdout = "  1 0\n  50 1\n  60 50\n  70 60\n  80 1\n"

    monkeypatch.setattr(proof.subprocess, "run", lambda *a, **k: Done())
    assert proof.ancestor_pids(70) == {70, 60, 50}


def test_feed_bundle_isolation_is_read_from_the_zip(tmp_path: Path) -> None:
    import zipfile

    layout = Layout(tmp_path / "root")
    info = {
        "CFBundleShortVersionString": "1.0.0-alpha.1.900",
        "LSEnvironment": proof.expected_environment(layout, 8765),
    }
    with zipfile.ZipFile(tmp_path / "OpenDJ-1.0.0-alpha.1.900-arm64-mac.zip", "w") as archive:
        archive.writestr("Open DJ.app/Contents/Info.plist", plistlib.dumps(info))
    subject, read = proof.feed_bundle_info(tmp_path, bridge=False)
    assert subject.endswith("-arm64-mac.zip") and read == info
    proof.check_environment(read, subject, layout, 8765)
    with pytest.raises(ProofError, match="does not isolate"):
        proof.check_environment(read, subject, Layout(tmp_path / "other"), 8765)


def test_escape_watch_fires_when_the_real_lock_changes(tmp_path: Path) -> None:
    real = tmp_path / ".engine.lock"
    watch = proof.EscapeWatch(real)
    watch.check()
    real.write_text("{}", encoding="utf-8")
    with pytest.raises(ProofError, match="REAL library lock"):
        watch.check()


def test_bridge_refuses_an_account_that_has_a_real_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    (home / "Library/Application Support/com.opendj.desktop").mkdir(parents=True)
    monkeypatch.setattr(proof.Path, "home", staticmethod(lambda: home))
    evidence = tmp_path / "evidence.json"
    code = proof.main(
        [
            "--bridge", "--n-app", str(tmp_path / "N.app"), "--feed", str(tmp_path / "feed"),
            "--expect-version", "1.0.0-alpha.1.950", "--root", str(home / "proof-1"), "--port", "8766",
            "--evidence", str(evidence),
        ]
    )  # fmt: skip
    assert code == 1 and not (home / "proof-1").exists()
    assert "no Open DJ files" in json.loads(evidence.read_text(encoding="utf-8"))["failure"]


def test_bridge_refuses_an_account_holding_any_app_trace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    backup = home / "Library/Application Support/com.opendj.desktop.backup"
    backup.mkdir(parents=True)
    monkeypatch.setattr(proof.Path, "home", staticmethod(lambda: home))
    with pytest.raises(ProofError, match="did not create"):
        proof.check_bridge_account(Layout(home / "proof-1", bridge=True, account_home=home))
    assert backup.is_dir()
