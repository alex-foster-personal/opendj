"""One-way import gate: nothing here may write toward real rekordbox data.

This suite is the review gate for W1-D.  It iterates
:data:`apps.shared.rekordbox_writeback.WRITE_SURFACES` three ways, so the map
cannot rot in either direction:

  * every mapped surface still CALLS the guard at its declared guard site
    (static) -- a refactor that drops a guard fails here;
  * every mapped surface REFUSES when the flag is off (behavioural) -- and it
    refuses with the one distinct error, never a silent no-op or fake success;
  * every module in ``apps/`` that names a live rekordbox target is either a
    mapped guard site or explicitly allowlisted as import-direction-only -- a
    new write path that nobody mapped fails here.

No live rekordbox database, share directory, or USB volume is touched: every
probe is refused before it can reach a filesystem target, which is precisely
the property under test.

NO @pytest.mark.requirement markers here on purpose: the proposed requirement
lines (SYNC-ONEWAY-01..04, see the W1-D handoff) are not in reqs.json yet, and
a marker for an unknown ID lands in coverage-matrix.md as an orphan. Add the
markers in the same change that adds the requirement lines.

Regression lines:
  - if a mapped surface stops calling require_writeback_enabled then broken
  - if any mapped surface answers 200/None while the flag is off then broken
  - if a new apps/ module names REKORDBOX_LIVE_DB without being mapped or
    allowlisted then broken
  - if the flag reads a typo like "ture" as either on or off then broken
  - if turning the flag on does not let the guard through then the code was
    deleted rather than disabled
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from apps.shared.rekordbox_writeback import (
    REKORDBOX_WRITEBACK_ENABLED_ENV,
    UI_REFUSAL_TITLE,
    WRITE_SURFACES,
    WRITEBACK_DISABLED_CODE,
    RekordboxWritebackDisabled,
    require_writeback_enabled,
    writeback_enabled,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

HTTP_SURFACES = tuple(s for s in WRITE_SURFACES if s.kind == "http")
MODULE_SURFACES = tuple(s for s in WRITE_SURFACES if s.kind == "module")

# Modules that NAME a live rekordbox target but only ever read from it, or
# copy it INTO the app. Import direction is the whole point of this gate, so
# these are allowed -- each with the reason it is allowed, so a future reader
# can audit the list instead of trusting it.
IMPORT_DIRECTION_ONLY: dict[str, str] = {
    "apps/shared/platform_paths.py": "defines the constants; touches nothing",
    "apps/shared/paths.py": "copy_live_dbs snapshots live -> data/, import direction",
    "apps/shared/rekordbox_db.py": "opens the WORKING copy; live path only in an error string",
    "apps/shared/library_integrity.py": "reads to report integrity",
    "apps/shared/state/ingest/rekordbox.py": "ingests rekordbox -> state.db, import direction",
    "apps/shared/rekordbox_writeback.py": "the gate itself",
    "apps/audit/session_history.py": "reads play history",
    "apps/sync/playlist_apply.py": "writes djay; reads the rb live db for TSAF leaf validation",
    "apps/webui/crate_sync.py": "reads SHARE_ROOT, writes the replica crate only",
    "apps/sync/usb/pioneer/reader.py": "reads an exportLibrary.db",
    "apps/sync/usb/pioneer/differ.py": "diffs two exportLibrary.db reads",
    "apps/sync/usb/pioneer/__init__.py": "package docstring",
    "apps/sync/usb/pioneer/writer_rbox.py": "reached only via the two mapped USB entrypoints",
}

LIVE_TARGET_MARKERS = ("REKORDBOX_LIVE_DB", "SHARE_ROOT", "exportLibrary")


@pytest.fixture(autouse=True)
def _gate_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts from the shipped default: writeback OFF."""
    monkeypatch.delenv(REKORDBOX_WRITEBACK_ENABLED_ENV, raising=False)


# ----- flag semantics -------------------------------------------------------


def test_flag_defaults_off_when_unset() -> None:
    assert writeback_enabled() is False


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "  OFF  "])
def test_flag_reads_falsey_values_as_off(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(REKORDBOX_WRITEBACK_ENABLED_ENV, raw)
    assert writeback_enabled() is False


@pytest.mark.parametrize("raw", ["1", "true", "YES", "on"])
def test_flag_reads_truthy_values_as_on(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(REKORDBOX_WRITEBACK_ENABLED_ENV, raw)
    assert writeback_enabled() is True


def test_flag_refuses_to_guess_at_a_typo(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo must not be read as "off" by luck, nor ever as "on"."""
    monkeypatch.setenv(REKORDBOX_WRITEBACK_ENABLED_ENV, "ture")
    with pytest.raises(ValueError, match="is not a boolean"):
        writeback_enabled()


def test_guard_rejects_a_surface_that_is_not_in_the_map() -> None:
    with pytest.raises(KeyError, match="unmapped rekordbox write surface"):
        require_writeback_enabled("module.some.brand.new.writer")


def test_guard_lets_a_mapped_surface_through_once_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KEPT, not deleted: turning the flag on restores every write path."""
    monkeypatch.setenv(REKORDBOX_WRITEBACK_ENABLED_ENV, "1")
    for surface in WRITE_SURFACES:
        require_writeback_enabled(surface.surface_id)


# ----- the map: static half -------------------------------------------------


@pytest.mark.parametrize("surface", WRITE_SURFACES, ids=lambda s: s.surface_id)
def test_every_mapped_surface_calls_the_guard_at_its_guard_site(surface) -> None:
    source = (REPO_ROOT / surface.guard_site).read_text(encoding="utf-8")
    expected = f'require_writeback_enabled("{surface.surface_id}")'
    assert expected in source, (
        f"{surface.guard_site} no longer calls {expected}; either restore the "
        "guard or remove the surface from WRITE_SURFACES"
    )


def test_surface_ids_are_unique() -> None:
    ids = [surface.surface_id for surface in WRITE_SURFACES]
    assert len(ids) == len(set(ids))


def test_no_unmapped_module_names_a_live_rekordbox_target() -> None:
    """A new write path that nobody wrote into the map fails review here."""
    guarded = {surface.guard_site for surface in WRITE_SURFACES}
    unaccounted: list[str] = []
    for path in sorted((REPO_ROOT / "apps").rglob("*.py")):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative in guarded or relative in IMPORT_DIRECTION_ONLY:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if any(marker in text for marker in LIVE_TARGET_MARKERS):
            unaccounted.append(relative)
    assert not unaccounted, (
        "these modules name a live rekordbox target but are neither a mapped "
        "guard site nor allowlisted as import-direction-only: "
        f"{unaccounted}. Add a WriteSurface (and a guard), or add an entry to "
        "IMPORT_DIRECTION_ONLY with the reason it only reads."
    )


def test_allowlist_has_no_stale_entries() -> None:
    missing = [
        relative for relative in IMPORT_DIRECTION_ONLY
        if not (REPO_ROOT / relative).is_file()
    ]
    assert not missing, f"IMPORT_DIRECTION_ONLY names files that no longer exist: {missing}"


# ----- the map: behavioural half, HTTP --------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """One in-memory playlist is all these probes need: the guard is the first
    statement in every handler, so nothing downstream is ever reached."""
    from apps.webui.server import rb_vendor
    from apps.webui.server.app import create_app
    from apps.webui.server.backend import InMemoryBackend, Playlist

    monkeypatch.setattr(rb_vendor, "playlist_order_index", dict)
    backend = InMemoryBackend()
    backend.seed_playlist(Playlist(playlist_id="pl-1", name="Warmup", vendor="rekordbox"))
    return TestClient(create_app(backend=backend))


def _http_probe(surface_id: str, client: TestClient, playlist_id: str):
    """Fire the smallest schema-valid request that reaches each handler."""
    writeback_apply = {
        "vendor": "rekordbox", "target_mode": "live", "target_path": "/nope.db",
        "target_id": "t-1", "plan_token": "tok", "dry_run": False, "confirmed": True,
    }
    writeback_rollback = {
        "vendor": "rekordbox", "target_mode": "live", "target_path": "/nope.db",
        "target_id": "t-1", "backup_id": "b-1",
        "expected_target_revision": "rev-1", "confirmed": True,
    }
    usb_plan = {
        "plan_id": "p-1", "schema_version": 1, "scope": "onelibrary_overlay_only",
        "template_path": "/nope/template.db", "template_sha256": "0" * 64,
        "target_root": "/Volumes/nope", "volume_label": "NOPE",
        "volume_uuid": "0000", "authorization_id": "auth-1",
        "output_relative_path": "PIONEER/rekordbox/exportLibrary.db",
        "playlists": [], "track_updates": [],
    }
    if surface_id == "http.playlists.writeback.apply":
        return client.post(f"/api/v1/playlists/{playlist_id}/writeback/apply", json=writeback_apply)
    if surface_id == "http.playlists.writeback.rollback":
        return client.post(
            f"/api/v1/playlists/{playlist_id}/writeback/rollback", json=writeback_rollback
        )
    if surface_id == "http.relocate.apply":
        return client.post(
            "/api/v1/relocate/no-such-track/apply",
            json={
                "new_path": "/nope/track.mp3", "expected_original_path": "/gone/track.mp3",
                "expected_vendor_id": "9", "expected_candidate_identity": "x", "confirm": True,
            },
            headers={"If-Match": "etag"},
        )
    if surface_id == "http.usb-export.apply":
        return client.post(
            "/api/v1/usb-export/apply", json={"plan": usb_plan, "confirmation": "p-1"}
        )
    raise AssertionError(f"no HTTP probe wired for {surface_id}")


@pytest.mark.parametrize("surface", HTTP_SURFACES, ids=lambda s: s.surface_id)
def test_http_surface_refuses_while_the_gate_is_off(surface, client: TestClient) -> None:
    response = _http_probe(surface.surface_id, client, "pl-1")
    assert response.status_code == 403, (
        f"{surface.entrypoint} answered {response.status_code}, not a refusal"
    )
    body = response.json()
    assert body["error"] == WRITEBACK_DISABLED_CODE
    assert "one-way import mode" in body["message"]
    assert body["details"]["surface"] == surface.surface_id


def test_read_only_writeback_surfaces_stay_reachable(client: TestClient) -> None:
    """Import direction is untouched: reads about rekordbox still answer."""
    response = client.get("/api/v1/playlists/pl-1/writeback/capabilities")
    assert response.status_code == 200


def test_gate_status_endpoint_reports_off_and_lists_every_surface(client: TestClient) -> None:
    body = client.get("/api/v1/rekordbox/writeback-gate").json()
    assert body["enabled"] is False
    assert body["code"] == WRITEBACK_DISABLED_CODE
    assert body["ui_title"] == UI_REFUSAL_TITLE
    assert body["env_var"] == REKORDBOX_WRITEBACK_ENABLED_ENV
    assert {surface["surface_id"] for surface in body["surfaces"]} == {
        surface.surface_id for surface in WRITE_SURFACES
    }


def test_gate_status_endpoint_follows_the_env(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REKORDBOX_WRITEBACK_ENABLED_ENV, "1")
    assert client.get("/api/v1/rekordbox/writeback-gate").json()["enabled"] is True


# ----- the map: behavioural half, modules -----------------------------------


def _probe_playlist_writeback_apply() -> None:
    from apps.webui.server import playlist_writeback as module

    plan = SimpleNamespace(
        plan_token="tok", unresolved=[], added=[], removed=[],
        target_name="Main", target_revision="rev", mapping_revision="map",
        source_revision="src",
    )
    service = module.WritebackService(writer_factory=_never_called_factory)
    object.__setattr__(service, "plan", lambda **_kwargs: plan)
    service.apply(
        vendor="rekordbox", source_playlist_id="pl-1", desired_ids=["a"],
        target_mode="live", target_path="/nope.db", target_id="t-1",
        plan_token="tok", dry_run=False, confirmed=True,
    )


def _probe_playlist_writeback_rollback() -> None:
    from apps.webui.server import playlist_writeback as module

    module.WritebackService(writer_factory=_never_called_factory).rollback(
        vendor="rekordbox", target_mode="live", target_path="/nope.db",
        target_id="t-1", backup_id="b-1", expected_target_revision="rev", confirmed=True,
    )


def _never_called_factory(*_args, **_kwargs):
    raise AssertionError("the gate must refuse before any writer is opened")


def _probe_smartlists_rb_writer() -> None:
    from apps.smartlists.rb_writer import RBPlaylistWriter

    writer = RBPlaylistWriter(
        db=object(), state_conn=sqlite3.connect(":memory:"), live=True,
        live_db_path=Path("/nope/master.db"),
    )
    writer._assert_safe_to_write()


def _probe_relocate_write_folder_path() -> None:
    from apps.webui.server.routes.relocate import _write_rekordbox_folder_path

    _write_rekordbox_folder_path("9", "/gone/track.mp3", object(), "identity")


def _probe_usb_pioneer_cli_write() -> None:
    from apps.sync.usb.pioneer.__main__ import _cmd_write

    _cmd_write(SimpleNamespace(
        playlist=[], track=[], template="/nope/t.db", output="/nope/o.db", apply=True,
    ))


def _probe_usb_pioneer_agent_export() -> None:
    from apps.sync.usb.pioneer.__main__ import _cmd_agent_export

    _cmd_agent_export(SimpleNamespace(
        playlist="Warmup", usb="/Volumes/nope", live=False, max_steps=1, verbose=False,
    ))


MODULE_PROBES: dict[str, Callable[[], None]] = {
    "module.relocate.write_folder_path": _probe_relocate_write_folder_path,
    "module.playlist_writeback.service_apply": _probe_playlist_writeback_apply,
    "module.playlist_writeback.service_rollback": _probe_playlist_writeback_rollback,
    "module.smartlists.rb_writer": _probe_smartlists_rb_writer,
    "module.sync.apply_ratings": lambda: __import__(
        "apps.sync.apply_ratings", fromlist=["_live_rb_db_path"]
    )._live_rb_db_path(True),
    "module.sync.apply_analysis": lambda: __import__(
        "apps.sync.apply_analysis", fromlist=["_live_rb_db_path"]
    )._live_rb_db_path(True),
    "module.sync.apply_cues": lambda: __import__(
        "apps.sync.apply_cues", fromlist=["live_run"]
    ).live_run([{"rb_content_id": "1"}], flag_ok=True, cautious=True),
    "module.sync.safety.live_write_session": lambda: __import__(
        "apps.sync.safety", fromlist=["LiveWriteSession"]
    ).LiveWriteSession(
        target="rekordbox", reason="probe", flag_ok=True, db_path=Path("/nope/master.db"),
    ).__enter__(),
    "module.reconcile.apply": lambda: __import__(
        "apps.reconcile.apply", fromlist=["_open_live_db"]
    )._open_live_db(),
    "module.reconcile.remove_track": lambda: __import__(
        "apps.reconcile.remove_track", fromlist=["_resolve_db_path"]
    )._resolve_db_path(None, live=True),
    "module.reconcile.prefix_dead_playlists": lambda: __import__(
        "apps.reconcile.prefix_dead_playlists", fromlist=["_apply"]
    )._apply([]),
    "module.sync.usb.pioneer.export_workflow": lambda: __import__(
        "apps.sync.usb.pioneer.export_workflow", fromlist=["apply_export"]
    ).apply_export(object(), confirmation="p-1"),
    "module.sync.usb.apply": lambda: __import__(
        "apps.sync.usb.apply", fromlist=["main"]
    ).main(["--profile", "/nope/profile.yaml", "--cautious"]),
    "module.sync.usb.pioneer.cli_write": _probe_usb_pioneer_cli_write,
    "module.sync.usb.pioneer.agent_export": _probe_usb_pioneer_agent_export,
}


def test_every_module_surface_has_a_probe() -> None:
    """A mapped module surface with no probe is an untested gate."""
    assert set(MODULE_PROBES) == {surface.surface_id for surface in MODULE_SURFACES}


@pytest.mark.parametrize("surface", MODULE_SURFACES, ids=lambda s: s.surface_id)
def test_module_surface_refuses_while_the_gate_is_off(surface) -> None:
    with pytest.raises(RekordboxWritebackDisabled) as caught:
        MODULE_PROBES[surface.surface_id]()
    assert caught.value.surface_id == surface.surface_id
    assert caught.value.code == WRITEBACK_DISABLED_CODE
    assert caught.value.to_dict()["surface"] == surface.surface_id
