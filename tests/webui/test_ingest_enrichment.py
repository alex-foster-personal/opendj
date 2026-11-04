"""Ingest enrichment coordinator (ENRICH-06..09).

The policy (lane states, artwork gate, capability warnings) is pure and tested
directly. The coordinator runs with injected sources: the only doubles are the
engine reads it is handed (lane facts, the drain's prioritise hook), never an
HTTP answer. The SQL helpers run against real sqlite files; the HTTP routes and
the CLI run against a real ``create_app`` engine.

Regression one-liners:
  - [if] a fresh batch is not put at the front of the analysis drain [then] broken, [else stop].
  - [if] rows from the folder rescan or first-run import never become a batch [then] broken, [else stop].
  - [if] artwork starts while any track of the batch still lacks a waveform [then] broken, [else stop].
  - [if] artwork starts while a deck plays [then] broken, [else stop].
  - [if] a failed, declined, placeholder or unavailable lane holds artwork forever [then] broken, [else stop].
  - [if] online lookups off, a lane unavailable or AI keys missing is not a named warning [then] broken, [else stop].
  - [if] one warning is logged on every tick [then] broken, [else stop].
  - [if] a rerun does not forget failures and cover misses [then] broken, [else stop].
  - [if] a placeholder own beatgrid (confidence 0) counts as a real grid [then] broken, [else stop].
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.opendj_cli import enrichment_cli
from apps.webui.server import ingest_enrichment as ie
from apps.webui.server.artwork_backfill import Outcome
from apps.webui.server.ingest_enrichment import (
    EnrichmentCoordinator,
    EnrichmentSources,
    EnrichWarning,
    LaneFacts,
)

pytestmark = pytest.mark.requirement("ENRICH-06")

BACKENDS = ie.LANE_BACKENDS


def facts(ids: Sequence[str], *, done_lanes: Sequence[str] = ("tags", "waveform", "beatgrid", "key", "loudness"),
          **over: object) -> LaneFacts:
    """Every id present and unmapped, with ``done_lanes`` produced for all of them."""
    ids = list(ids)
    done = {backend: (set(ids) if lane in done_lanes else set()) for lane, backend in BACKENDS.items()}
    base: dict[str, object] = dict(
        present=set(ids),
        blank_tags=set() if "tags" in done_lanes else set(ids),
        mapped=set(),
        has_strip=set(ids) if "waveform" in done_lanes else set(),
        done=done,
        declined={},
        placeholder=set(),
    )
    base.update(over)
    return LaneFacts(**base)  # type: ignore[arg-type]


class FakeCovers:
    def __init__(self) -> None:
        self.scheduled: list[str] = []

    def schedule(self, ids: Sequence[str]) -> int:
        self.scheduled.extend(ids)
        return len(ids)


class Rig:
    """A coordinator over mutable fake engine reads."""

    def __init__(self, tmp_path: Path) -> None:
        self.facts: LaneFacts = facts([])
        self.rows: list[str] = []
        self.playing = False
        self.prioritized: list[list[str]] = []
        self.retried: list[str] = []
        self.forgotten: list[str] = []
        self.warnings: list[EnrichWarning] = []
        self.playlists: dict[str, list[str]] = {}
        self.covers = FakeCovers()
        self.store = tmp_path / "state" / ie.STORE_FILENAME
        self.coordinator = self.build()

    def build(self) -> EnrichmentCoordinator:
        return EnrichmentCoordinator(
            EnrichmentSources(
                facts_fn=lambda _ids: self.facts,
                new_rows_fn=lambda mark: ([sid for i, sid in enumerate(self.rows) if i + 1 > mark], len(self.rows)),
                playlist_ids_fn=lambda pid: list(self.playlists[pid]),
                warnings_fn=lambda: list(self.warnings),
                playing_fn=lambda: self.playing,
                prioritize_fn=lambda ids: self.prioritized.append(list(ids)),
                retry_fn=self.retried.extend,
                forget_misses_fn=self.forgotten.extend,
                store_path=self.store,
            ),
            covers=self.covers,  # type: ignore[arg-type]
            clock=lambda: 1_000.0,
        )


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


#-----------------------------------------------------------------------------
# ENRICH-06: every import is enriched, front of the queue
#-----------------------------------------------------------------------------
def test_registered_batch_goes_to_the_front_of_the_drain(rig: Rig) -> None:
    rig.coordinator.register("viva", ["a", "b"], "materialize")
    assert rig.prioritized == [["a", "b"]]
    assert rig.coordinator.status()["batches"][0]["batch_id"] == "viva"


def test_rows_from_another_import_path_become_an_auto_batch(rig: Rig) -> None:
    rig.rows = ["old1", "old2"]
    assert rig.coordinator.reconcile_new_rows() is None, "first sight is the library as it is, not new"
    rig.rows += ["new1", "new2"]
    batch = rig.coordinator.reconcile_new_rows()
    assert batch is not None and batch.batch_id.startswith(ie.AUTO_PREFIX)
    assert batch.stable_ids == ["new1", "new2"]
    assert rig.prioritized[-1] == ["new1", "new2"]


def test_named_batch_takes_ids_an_auto_batch_already_picked_up(rig: Rig) -> None:
    rig.coordinator.reconcile_new_rows()
    rig.rows = ["x", "y"]
    rig.coordinator.reconcile_new_rows()
    rig.coordinator.register("drop", ["x", "y"], "materialize")
    assert [b["batch_id"] for b in rig.coordinator.status()["batches"]] == ["drop"]


def test_batches_and_watermark_survive_a_restart(rig: Rig) -> None:
    rig.rows = ["a"]
    rig.coordinator.reconcile_new_rows()
    rig.coordinator.register("viva", ["a"], "materialize")
    again = rig.build()
    assert [b["batch_id"] for b in again.status()["batches"]] == ["viva"]
    assert again.reconcile_new_rows() is None, "the stored watermark means nothing is new"


#-----------------------------------------------------------------------------
# ENRICH-07: artwork waits for the essentials
#-----------------------------------------------------------------------------
@pytest.mark.requirement("ENRICH-07")
def test_artwork_waits_while_any_track_lacks_a_waveform(rig: Rig) -> None:
    rig.coordinator.register("viva", ["a", "b"], "materialize")
    done = facts(["a", "b"])
    rig.facts = LaneFacts(**{**done.__dict__, "has_strip": {"a"}})
    assert rig.coordinator.tick() == []
    assert rig.covers.scheduled == []
    lanes = rig.coordinator.status("viva", with_tracks=True)["batches"][0]["track_lanes"]
    assert lanes["b"]["waveform"]["state"] == "pending"
    assert lanes["b"]["artwork"]["state"] == "pending"
    rig.facts = done
    assert rig.coordinator.tick() == ["viva"]
    assert rig.covers.scheduled == ["a", "b"]
    assert rig.coordinator.tick() == [], "a released batch is queued once"


@pytest.mark.requirement("ENRICH-07")
def test_artwork_waits_while_a_deck_plays(rig: Rig) -> None:
    rig.coordinator.register("viva", ["a"], "materialize")
    rig.facts = facts(["a"])
    rig.playing = True
    assert rig.coordinator.tick() == []
    rig.playing = False
    assert rig.coordinator.tick() == ["viva"]


@pytest.mark.requirement("ENRICH-07")
def test_settled_but_unfinished_lanes_do_not_hold_artwork(rig: Rig) -> None:
    ids = ["fail", "decl", "ph", "gone"]
    rig.coordinator.register("viva", ids, "materialize")
    base = facts(ids, done_lanes=("tags", "waveform", "beatgrid", "loudness"))
    rig.facts = LaneFacts(**{
        **base.__dict__,
        "present": {"fail", "decl", "ph"},
        "done": {**base.done, BACKENDS["key"]: {"decl", "ph"}},
        "declined": {"key": {"decl": "no_tonal_center"}},
        "placeholder": {"ph"},
        "failures": {"key": {"fail": "decode failed"}},
    })
    assert rig.coordinator.tick() == ["viva"]
    lanes = rig.coordinator.status("viva", with_tracks=True)["batches"][0]["track_lanes"]
    assert lanes["fail"]["key"] == {"state": "failed", "reason": "decode failed"}
    assert lanes["decl"]["key"]["state"] == "declined"
    assert lanes["ph"]["beatgrid"]["state"] == "placeholder"
    assert lanes["gone"]["waveform"]["state"] == "absent"


@pytest.mark.requirement("ENRICH-07")
def test_a_stopped_drain_reports_unavailable_and_does_not_strand_artwork(rig: Rig) -> None:
    rig.coordinator.register("viva", ["a"], "materialize")
    rig.facts = LaneFacts(**{**facts(["a"], done_lanes=()).__dict__, "drain_running": False})
    assert rig.coordinator.tick() == ["viva"]
    row = rig.coordinator.status("viva", with_tracks=True)["batches"][0]["track_lanes"]["a"]
    assert row["beatgrid"]["state"] == "unavailable"


@pytest.mark.requirement("ENRICH-07")
def test_cover_outcomes_show_per_track(rig: Rig) -> None:
    rig.coordinator.register("viva", ["a", "b"], "materialize")
    rig.facts = facts(["a", "b"])
    rig.coordinator.tick()
    rig.coordinator._record_cover("a", Outcome("found", "online cover cached"))
    rig.coordinator._record_cover("b", Outcome("miss", "no matching recording"))
    batch = rig.coordinator.status("viva")["batches"][0]
    assert batch["lanes"]["artwork"] == {"done": 1, "failed": 1}
    assert batch["artwork_stage"] == "queued"
    rig.coordinator._record_cover("b", Outcome("suggested", "deezer has a cover"))
    assert rig.coordinator.status("viva")["batches"][0]["lanes"]["artwork"] == {"done": 1, "suggested": 1}


def test_lane_priority_is_essentials_then_artwork() -> None:
    assert ie.LANE_PRIORITY == ("tags", "waveform", "beatgrid", "key", "loudness", "artwork")


#-----------------------------------------------------------------------------
# ENRICH-08: nothing missing is silent
#-----------------------------------------------------------------------------
@pytest.mark.requirement("ENRICH-08")
def test_capability_warnings_name_what_is_missing_and_where() -> None:
    warnings = ie.capability_warnings(drain_running=False, unavailable={"loudness": "ffmpeg has no astats"},
                                      artwork_online=False, ai_missing_keys=["OPENROUTER_API_KEY"])
    by_code = {w.code: w for w in warnings}
    assert set(by_code) == {"ANALYSIS_DRAIN_OFF", "LANE_UNAVAILABLE", "ARTWORK_ONLINE_OFF", "AI_GENRE_KEYS_MISSING"}
    assert "ODJ_ARTWORK_ONLINE=0" in by_code["ARTWORK_ONLINE_OFF"].fix
    assert "OPENROUTER_API_KEY" in by_code["AI_GENRE_KEYS_MISSING"].message
    assert by_code["AI_GENRE_KEYS_MISSING"].severity == "info"
    assert by_code["LANE_UNAVAILABLE"].severity == "blocking"
    assert ie.capability_warnings(drain_running=True, unavailable={}, artwork_online=True, ai_missing_keys=[]) == []


@pytest.mark.requirement("ENRICH-08")
def test_a_warning_is_logged_once(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    rig.warnings = ie.capability_warnings(drain_running=True, unavailable={}, artwork_online=False, ai_missing_keys=[])
    with caplog.at_level(logging.WARNING, logger=ie.__name__):
        rig.coordinator.tick()
        rig.coordinator.tick()
        rig.coordinator.status()
    assert sum("ARTWORK_ONLINE_OFF" in r.getMessage() for r in caplog.records) == 1
    assert rig.coordinator.status()["warnings"][0]["code"] == "ARTWORK_ONLINE_OFF"


#-----------------------------------------------------------------------------
# ENRICH-09: rerun, HTTP and CLI
#-----------------------------------------------------------------------------
@pytest.mark.requirement("ENRICH-09")
def test_rerun_playlist_forgets_failures_and_misses_and_reprioritises(rig: Rig) -> None:
    rig.playlists["pl1"] = ["a", "b"]
    out = rig.coordinator.rerun(playlist_id="pl1")
    assert out == {"ok": True, "batch_id": "playlist-pl1", "tracks": 2}
    assert rig.retried == ["a", "b"] and rig.forgotten == ["a", "b"]
    assert rig.prioritized[-1] == ["a", "b"]


@pytest.mark.requirement("ENRICH-09")
def test_rerun_needs_exactly_one_target(rig: Rig) -> None:
    with pytest.raises(ValueError):
        rig.coordinator.rerun()
    with pytest.raises(KeyError):
        rig.coordinator.rerun(batch_id="nope")


def _db(path: Path, ddl: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(ddl)
    return conn


def test_placeholder_grid_ids_reads_zero_confidence_records(tmp_path: Path) -> None:
    from apps.webui.server.ahead_analysis_records import producer_version

    path = tmp_path / "a.db"
    conn = _db(path, "CREATE TABLE analysis (stable_id TEXT, backend TEXT, backend_version TEXT, record_json TEXT);")
    backend, version = BACKENDS["beatgrid"], producer_version(BACKENDS["beatgrid"])

    def record(bpm: float, conf: float) -> str:
        return json.dumps({"lanes": {"beatgrid": {"status": "ok", "payload": {"bpm": bpm, "bpm_confidence": conf}}}})

    conn.executemany("INSERT INTO analysis VALUES (?, ?, ?, ?)", [
        ("real", backend, version, record(124.0, 0.9)),
        ("zero", backend, version, record(120.0, 0.0)),
        ("nobpm", backend, version, record(0.0, 0.5)),
        ("stale", backend, "old-version", record(120.0, 0.0)),
    ])
    conn.commit()
    conn.close()
    assert ie.placeholder_grid_ids(lambda: sqlite3.connect(path)) == {"zero", "nobpm"}


def test_new_rows_and_playlist_members_read_live_rows(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    audio = tmp_path / "x.mp3"
    audio.write_bytes(b"x")
    conn = _db(path, """
        CREATE TABLE tracks (stable_id TEXT PRIMARY KEY, file_path TEXT, deleted_at TEXT);
        CREATE TABLE playlists (playlist_id TEXT, deleted_at TEXT);
        CREATE TABLE playlist_memberships (playlist_id TEXT, stable_id TEXT, position INTEGER, deleted_at TEXT);
    """)
    conn.executemany("INSERT INTO tracks VALUES (?, ?, ?)",
                     [("a", str(audio), None), ("b", str(tmp_path / "gone.mp3"), None), ("c", None, "2026")])
    conn.execute("INSERT INTO playlists VALUES ('pl', NULL)")
    conn.executemany("INSERT INTO playlist_memberships VALUES ('pl', ?, ?, ?)",
                     [("b", 1, None), ("a", 0, None), ("c", 2, "2026")])
    conn.commit()
    conn.close()
    factory = lambda: sqlite3.connect(path)  # noqa: E731
    assert ie.new_track_rows(factory, 1) == (["b"], 3)
    assert ie.present_ids(factory, ["a", "b", "c"]) == {"a"}
    assert ie.playlist_member_ids(factory, "pl") == ["a", "b"]
    with pytest.raises(KeyError):
        ie.playlist_member_ids(factory, "missing")


@pytest.fixture
def engine(tmp_path: Path, rig: Rig):
    from apps.webui.server.app import create_app

    app = create_app(mount_frontend=False, enable_cors=False)
    app.state.ingest_enrichment = rig.coordinator
    return TestClient(app)


@pytest.mark.requirement("ENRICH-09")
def test_http_status_batch_and_rerun(engine: TestClient, rig: Rig) -> None:
    rig.facts = facts(["a"])
    rig.coordinator.register("viva", ["a"], "materialize")
    listing = engine.get("/api/v1/ingest/enrichment").json()
    assert listing["lane_priority"][-1] == "artwork"
    assert listing["batches"][0]["lanes"]["beatgrid"] == {"done": 1}
    one = engine.get("/api/v1/ingest/enrichment/viva").json()
    assert one["batches"][0]["track_lanes"]["a"]["key"]["state"] == "done"
    assert engine.get("/api/v1/ingest/enrichment/nope").status_code == 404
    assert engine.post("/api/v1/ingest/enrichment/rerun", json={}).status_code == 422
    assert engine.post("/api/v1/ingest/enrichment/rerun", json={"playlist_id": "nope"}).status_code == 404
    assert engine.post("/api/v1/ingest/enrichment/rerun", json={"batch_id": "viva"}).json()["ok"] is True


@pytest.mark.requirement("ENRICH-08")
def test_enrich_summary_carries_the_warnings(engine: TestClient, rig: Rig) -> None:
    rig.warnings = ie.capability_warnings(drain_running=True, unavailable={}, artwork_online=False, ai_missing_keys=[])
    summary = engine.get("/api/v1/enrich/summary").json()
    assert [w["code"] for w in summary["warnings"]] == ["ARTWORK_ONLINE_OFF"]
    assert summary["show"] is True


@pytest.mark.requirement("ENRICH-09")
def test_cli_request_shapes() -> None:
    assert enrichment_cli.request_for("status", None, None) == ("GET", "/api/v1/ingest/enrichment", None)
    assert enrichment_cli.request_for("status", "a b", None)[1] == "/api/v1/ingest/enrichment/a%20b"
    assert enrichment_cli.request_for("rerun", None, "pl") == (
        "POST", "/api/v1/ingest/enrichment/rerun", {"playlist_id": "pl"})
    with pytest.raises(ValueError):
        enrichment_cli.request_for("rerun", "b", "pl")


@pytest.mark.requirement("ENRICH-09")
def test_cli_with_no_engine_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apps.opendj_cli import EXIT_NO_ENGINE
    from apps.opendj_cli.__main__ import main

    monkeypatch.delenv("MDT_API_PORT", raising=False)
    assert main(["--lock", str(tmp_path / "missing.lock"), "enrichment", "status"]) == EXIT_NO_ENGINE
