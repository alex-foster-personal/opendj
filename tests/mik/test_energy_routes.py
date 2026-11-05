"""Energy coverage and backfill over HTTP (MIKBF-04).

- [if] GET /energy/status runs [then] its buckets sum to the named present denominator, [else stop].
- [if] stage or collect is posted without live [then] nothing is copied, written or deleted, [else stop].
- [if] collect names a folder stage could not have made [then] it answers 422 before reading anything, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.mik import load as loader
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.mik.backfill_support import SEGMENTS, Lib, sid

pytestmark = pytest.mark.requirement("MIKBF-04")

API = "/api/v1/energy"


def _client(lib: Lib, *, peer_holds_lock: bool = False) -> TestClient:
    state = lib.library.state_db
    app = create_app(
        backend=SqliteBackend(state),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state),
        mount_frontend=False,
        lock_status_fn=(lambda: {"holder": "another-host"}) if peer_holds_lock else None,
    )
    return TestClient(app)


def _energy(lib: Lib) -> list[tuple[str, str]]:
    return lib.conn.execute(
        "SELECT stable_id, value_json FROM track_fields WHERE field_name = 'energy' ORDER BY stable_id"
    ).fetchall()


def test_status_names_the_denominator_and_its_buckets_sum(lib: Lib) -> None:
    lib.track("a", "Music/a.mp3")
    lib.track("b", "Music/b.mp3")
    lib.track("c", "Music/c.mp3", state="absent", on_disk=False)
    lib.energy("a", 7)
    lib.series("a")
    with _client(lib) as client:
        resp = client.get(f"{API}/status")
        assert resp.status_code == 200, resp.text
        body = resp.json()
    assert body["denominator"] == "present" and body["present"] == 2
    without = body["without_mik_energy"]
    assert body["with_mik_energy"] == 1 and without["needs_fresh_analysis"] == 1
    assert body["with_mik_energy"] + sum(without.values()) == body["present"]
    assert body["staged_unmatched"] == {}


def test_plan_lists_the_next_files(lib: Lib, ingest_cfg: Path) -> None:
    lib.track("a", "Music/a.mp3")
    lib.track("b", "Music/b.mp3")
    with _client(lib) as client:
        body = client.post(f"{API}/backfill/plan", json={"limit": 1}).json()
        assert client.post(f"{API}/backfill/plan", json={"limit": 0}).status_code == 422
    assert body["to_analyze"]["files"] == 2
    assert [entry["stable_ids"] for entry in body["next"]] == [[sid("a")]]


def test_stage_is_a_dry_run_until_live(lib: Lib, ingest_cfg: Path) -> None:
    original = lib.track("a", "Music/a.mp3")
    with _client(lib) as client:
        dry = client.post(f"{API}/backfill/stage", json={})
        assert dry.status_code == 200, dry.text
        assert dry.json()["mode"] == "dry-run" and not ingest_cfg.exists()
        live = client.post(f"{API}/backfill/stage", json={"live": True}).json()
    batch = Path(live["batch_dir"])
    assert batch.parent == ingest_cfg and (batch / "a.mp3").read_bytes() == original.read_bytes()
    assert live["open_argv"] == ["open", "-a", "Mixed In Key 11", str(batch / "a.mp3")]
    assert live["open_command"].startswith("open -a 'Mixed In Key 11' ")


def test_collect_dry_run_then_live_then_delete(lib: Lib, ingest_cfg: Path, make_mik_store, write_verdicts) -> None:
    lib.track("a", "Music/a.mp3")
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    with _client(lib) as client:
        batch = Path(client.post(f"{API}/backfill/stage", json={"live": True}).json()["batch_dir"])
        store = make_mik_store([{"path": str(batch / "a.mp3"), "energy": 7.0, "segments": SEGMENTS}])
        body = {"batch_dir": str(batch), "store": str(store), "delete_copies": True}
        dry = client.post(f"{API}/backfill/collect", json=body)
        assert dry.status_code == 200, dry.text
        assert dry.json()["mode"] == "dry-run" and dry.json()["planned"]["field_writes"] == 1
        assert _energy(lib) == [] and (batch / "a.mp3").is_file()
        live = client.post(f"{API}/backfill/collect", json={**body, "live": True}).json()
        assert live["applied"]["fields_written"] == 1 and live["copies"]["deleted"] == 1
        assert _energy(lib) == [(sid("a"), "7")] and not (batch / "a.mp3").exists()
        assert client.get(f"{API}/status").json()["with_mik_energy"] == 1


def test_collect_reports_the_gate_and_offers_no_override(lib: Lib, ingest_cfg: Path, make_mik_store) -> None:
    """No verdict file: nothing is written, and the body cannot say otherwise."""
    lib.track("a", "Music/a.mp3")
    with _client(lib) as client:
        batch = Path(client.post(f"{API}/backfill/stage", json={"live": True}).json()["batch_dir"])
        store = make_mik_store([{"path": str(batch / "a.mp3"), "energy": 7.0, "segments": SEGMENTS}])
        body = {"batch_dir": str(batch), "store": str(store), "live": True, "delete_copies": True}
        forced = {**body, "allow_unverified": True, "i_know_equivalence_is_unverified": True}
        out = client.post(f"{API}/backfill/collect", json=forced).json()
    assert out["blocked_by_gate"] == {"energy": 1, "energy_segments": 1}
    assert out["applied"]["fields_written"] == 0 and out["copies"]["deleted"] == 0
    assert _energy(lib) == [] and (batch / "a.mp3").is_file()


def test_collect_refuses_a_folder_stage_could_not_have_made(lib: Lib, ingest_cfg: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "mik-batch-elsewhere"
    elsewhere.mkdir()
    (ingest_cfg / "not-a-batch").mkdir(parents=True)
    linked = ingest_cfg / "mik-batch-linked"  # a batch-shaped link to a folder outside the ingest root
    linked.symlink_to(elsewhere, target_is_directory=True)
    with _client(lib) as client:
        for folder in (elsewhere, ingest_cfg / "not-a-batch", linked):
            resp = client.post(f"{API}/backfill/collect", json={"batch_dir": str(folder), "store": "/nope"})
            assert resp.status_code == 422, resp.text
            assert resp.json()["detail"]["code"] == "not_a_batch_folder"


def test_a_refusal_is_a_409_carrying_the_reason(lib: Lib, ingest_cfg: Path) -> None:
    (ingest_cfg / "mik-batch-20261013-0000").mkdir(parents=True)
    with _client(lib) as client:
        body = {"batch_dir": str(ingest_cfg / "mik-batch-20261013-0000"), "store": "/nope/x.mikdb"}
        resp = client.post(f"{API}/backfill/collect", json=body)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "energy_backfill_refused"
    assert ".batch.json" in resp.json()["detail"]["message"]


def test_a_malformed_settings_file_is_a_409_not_a_crash(lib: Lib, ingest_cfg: Path) -> None:
    """if collect answers a broken mik-backfill.json with a 500 then broken."""
    from apps.mik import backfill_settings

    batch = ingest_cfg / "mik-batch-20261013-0000"
    batch.mkdir(parents=True)
    backfill_settings.settings_path(lib.library.data_dir).write_text("{not json", encoding="utf-8")
    with _client(lib) as client:
        resp = client.post(f"{API}/backfill/collect", json={"batch_dir": str(batch), "store": "/nope/x.mikdb"})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "energy_backfill_refused"
    assert "not valid JSON" in resp.json()["detail"]["message"]


def test_live_stage_honors_the_writer_lock_and_a_dry_run_does_not_need_it(lib: Lib, ingest_cfg: Path) -> None:
    """if a live stage copies files while another host holds the writer lock then broken."""
    state = lib.library.state_db
    app = create_app(
        backend=SqliteBackend(state),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state),
        mount_frontend=False,
        lock_status_fn=lambda: {"holder": "another-host"},
    )
    with TestClient(app) as client:
        dry = client.post(f"{API}/backfill/stage", json={"live": False})
        live = client.post(f"{API}/backfill/stage", json={"live": True})
    assert dry.status_code == 200, dry.text
    assert live.status_code == 503, live.text
    assert live.json()["detail"]["error"] == "locked_by_peer"
    assert not any(ingest_cfg.glob("mik-batch-*")) if ingest_cfg.exists() else True


def test_a_dry_collect_needs_no_writer_lock_and_a_live_one_honors_it(
    lib: Lib, ingest_cfg: Path, make_mik_store, write_verdicts
) -> None:
    """if a dry-run collect is refused while a peer holds the writer lock, or a live one is not, then broken."""
    lib.track("a", "Music/a.mp3")
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    with _client(lib) as client:
        batch = Path(client.post(f"{API}/backfill/stage", json={"live": True}).json()["batch_dir"])
    store = make_mik_store([{"path": str(batch / "a.mp3"), "energy": 7.0, "segments": SEGMENTS}])
    body = {"batch_dir": str(batch), "store": str(store), "delete_copies": True}
    with _client(lib, peer_holds_lock=True) as client:
        dry = client.post(f"{API}/backfill/collect", json=body)
        live = client.post(f"{API}/backfill/collect", json={**body, "live": True})
    assert dry.status_code == 200, dry.text
    assert dry.json()["mode"] == "dry-run" and dry.json()["planned"]["field_writes"] == 1
    assert live.status_code == 503, live.text
    assert live.json()["detail"]["error"] == "locked_by_peer"
    assert _energy(lib) == [] and (batch / "a.mp3").is_file()


@pytest.mark.requirement("MIKBF-08")
def test_share_route_fills_a_duplicate_and_takes_the_lock_only_when_live(lib: Lib, write_verdicts) -> None:
    """if a dry share is refused under a peer lock, a live one is not, or the route's document differs from the engine's, then broken."""
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    lib.track("a", "Music/x.mp3", stored="/Users/old/a/x.mp3")
    lib.track("b", "Music/x.mp3", stored="/Users/old/b/x.mp3")
    lib.energy("a", 7)
    lib.series("a")
    with _client(lib, peer_holds_lock=True) as client:
        dry = client.post(f"{API}/backfill/share", json={})
        locked = client.post(f"{API}/backfill/share", json={"live": True})
    assert dry.status_code == 200, dry.text
    assert dry.json()["counts"]["rows_to_fill"] == 1 and dry.json()["applied"] is None
    assert locked.status_code == 503 and locked.json()["detail"]["error"] == "locked_by_peer"
    with _client(lib) as client:
        live = client.post(f"{API}/backfill/share", json={"live": True})
        assert live.status_code == 200, live.text
        assert live.json()["applied"]["fields_written"] == 1
        assert client.get(f"{API}/status").json()["with_mik_energy"] == 2


@pytest.mark.parametrize("manifest", ["{not json", "[]", '{"other": 1}', '{"library_root": 7}'])
def test_a_malformed_batch_manifest_is_a_409_not_a_crash(lib: Lib, ingest_cfg: Path, manifest: str) -> None:
    """if a mik-batch-* folder with a broken .batch.json makes collect answer 500 then broken."""
    batch = ingest_cfg / "mik-batch-20261015-0000"
    batch.mkdir(parents=True)
    (batch / ".batch.json").write_text(manifest, encoding="utf-8")
    with _client(lib) as client:
        resp = client.post(f"{API}/backfill/collect", json={"batch_dir": str(batch), "store": "/nope"})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "energy_backfill_refused"
    assert ".batch.json" in resp.json()["detail"]["message"]
