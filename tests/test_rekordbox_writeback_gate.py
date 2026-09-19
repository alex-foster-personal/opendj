"""One-way import gate: nothing here may write toward real rekordbox data.

This suite is the review gate for W1-D.  It iterates
:data:`apps.shared.rekordbox_writeback.WRITE_SURFACES` four ways, so the map
cannot rot in either direction:

  * every GATED surface still CALLS the guard at its declared guard site
    (static) -- a refactor that drops a guard fails here.  Every UNGATED
    surface must NOT call it, so a deliberate, argued exception (rollback)
    cannot be quietly re-gated either;
  * every gated surface REFUSES when the flag is off (behavioural) -- with the
    one distinct error, never a silent no-op or fake success;
  * every module in ``apps/`` that NAMES a live rekordbox target is either a
    mapped guard site or explicitly allowlisted as import-direction-only;
  * every module in ``apps/`` that CONSTRUCTS a rekordbox DB handle is either
    a mapped guard site or explicitly allowlisted as read-only, with a reason.

The last sweep is the structural one.  A marker sweep can only see writers
that name a path constant, so it is blind to a writer that takes its path from
an argument or a CLI flag, and completely blind to one handed an already-open
handle.  Both shapes existed in this tree and both were live-write lanes
sitting behind a green suite (``apps/dedup/apply.py --rb-db``,
``apps.sync.rb_writer.write_cues(db, ...)``).

No live rekordbox database, share directory, or USB volume is touched. Every
path a probe builds comes from ``tmp_path``, and every probe is refused before
it can reach a filesystem target anyway -- which is precisely the property
under test. A fixture that names a real library location is how a suite like
this eventually writes to one by accident, so there are none here; the ban is
enforced by ``test_no_gate_test_can_name_a_real_library_location`` in
tests/test_rekordbox_writeback_surfaces.py.

Requirement markers live on the module-level ``pytestmark`` below. They were
added alongside the SYNC-ONEWAY requirement lines in e192ee9a; this docstring
described them as still pending long after that landed.

Regression lines:
  - if a gated surface stops calling require_writeback_enabled then broken
  - if an ungated surface starts calling it then the argued exception was
    reverted by accident, so broken
  - if any gated surface answers 200/None while the flag is off then broken
  - if a new apps/ module names REKORDBOX_LIVE_DB without being mapped or
    allowlisted then broken
  - if a new apps/ module opens a rekordbox DB handle without being mapped or
    read-only allowlisted then broken
  - if remove_track --db <path> reaches a DB path without passing the gate
    then broken
  - if a `..`-alias or a symlink of a --db target refuses differently from the
    plain path then the gate has become path-shaped, so broken
  - if rollback refuses while the gate is off then undo was taken away from a
    user who already has a bad write, so broken
  - if a backup_id that is not uuid4().hex reaches a path join then broken
  - if a crate --map can aim a local copy outside the crate root then broken
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

# 01 is the inventory-and-refusal contract this suite iterates four ways;
# 04 is the surface-not-path rule the remove_track alias and symlink probes
# below pin down.
pytestmark = [
    pytest.mark.requirement("SYNC-ONEWAY-01"),
    pytest.mark.requirement("SYNC-ONEWAY-04"),
]

REPO_ROOT = Path(__file__).resolve().parents[1]

GATED = tuple(s for s in WRITE_SURFACES if s.gated)
UNGATED = tuple(s for s in WRITE_SURFACES if not s.gated)
HTTP_SURFACES = tuple(s for s in GATED if s.kind == "http")
MODULE_SURFACES = tuple(s for s in GATED if s.kind == "module")



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
    for surface in GATED:
        require_writeback_enabled(surface.surface_id)


@pytest.mark.parametrize("surface", UNGATED, ids=lambda s: s.surface_id)
def test_guard_refuses_to_gate_a_deliberately_ungated_surface(surface) -> None:
    """An argued exception may only be reverted in the map, never at a call site."""
    with pytest.raises(KeyError, match="deliberately"):
        require_writeback_enabled(surface.surface_id)


def test_every_ungated_surface_states_its_reason() -> None:
    for surface in UNGATED:
        assert len(surface.reason) > 40, (
            f"{surface.surface_id} is ungated with no argument for why; an "
            "exception to a data-safety gate has to carry its reasoning"
        )


# ----- the map: static half -------------------------------------------------


@pytest.mark.parametrize("surface", GATED, ids=lambda s: s.surface_id)
def test_every_gated_surface_calls_the_guard_at_its_guard_site(surface) -> None:
    source = (REPO_ROOT / surface.guard_site).read_text(encoding="utf-8")
    expected = f'require_writeback_enabled("{surface.surface_id}")'
    assert expected in source, (
        f"{surface.guard_site} no longer calls {expected}; either restore the "
        "guard or remove the surface from WRITE_SURFACES"
    )


@pytest.mark.parametrize("surface", UNGATED, ids=lambda s: s.surface_id)
def test_every_ungated_surface_has_no_guard_at_its_site(surface) -> None:
    """The exception is deliberate; re-gating it must be a decision, not a diff."""
    source = (REPO_ROOT / surface.guard_site).read_text(encoding="utf-8")
    unwanted = f'require_writeback_enabled("{surface.surface_id}")'
    assert unwanted not in source, (
        f"{surface.guard_site} gates {surface.surface_id}, which is mapped as "
        f"deliberately ungated ({surface.reason}). Flip gated=True in the map "
        "if that decision has genuinely changed."
    )


def test_surface_ids_are_unique() -> None:
    ids = [surface.surface_id for surface in WRITE_SURFACES]
    assert len(ids) == len(set(ids))


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


def _http_probe(surface_id: str, client: TestClient, playlist_id: str, tmp: Path):
    """Fire the smallest schema-valid request that reaches each handler.

    Every path in every body is built under ``tmp``. Nothing here may name a
    real library location even in a string that is never opened: the guard is
    the first statement in each handler, so these bodies are only ever meant to
    get past request validation, and a plausible-looking real path in a fixture
    is exactly how a probe eventually gets pointed at the actual database.
    """
    writeback_apply = {
        "vendor": "rekordbox", "target_mode": "live", "target_path": str(tmp / "nope.db"),
        "target_id": "t-1", "plan_token": "tok", "dry_run": False, "confirmed": True,
    }
    usb_plan = {
        "plan_id": "p-1", "schema_version": 1, "scope": "onelibrary_overlay_only",
        "template_path": str(tmp / "template.db"), "template_sha256": "0" * 64,
        # A RELATIVE layout string the schema requires, joined against the tmp
        # target_root above and nothing else.
        "target_root": str(tmp / "volume"), "volume_label": "NOPE",
        "volume_uuid": "0000", "authorization_id": "auth-1",
        "output_relative_path": "PIONEER/rekordbox/exportLibrary.db",
        "playlists": [], "track_updates": [],
    }
    if surface_id == "http.playlists.writeback.apply":
        return client.post(f"/api/v1/playlists/{playlist_id}/writeback/apply", json=writeback_apply)
    if surface_id == "http.relocate.apply":
        return client.post(
            "/api/v1/relocate/no-such-track/apply",
            json={
                "new_path": str(tmp / "track.mp3"),
                "expected_original_path": str(tmp / "gone.mp3"),
                "expected_vendor_id": "9", "expected_candidate_identity": "x", "confirm": True,
            },
            headers={"If-Match": "etag"},
        )
    if surface_id == "http.usb-export.apply":
        return client.post(
            "/api/v1/usb-export/apply", json={"plan": usb_plan, "confirmation": "p-1"}
        )
    live_apply = {
        "dry_run": False,
        "live": True,
        "i_understand_the_risks": True,
        "diff_csv": str(tmp / "diff.csv"),
    }
    if surface_id == "http.rb_djay_sync.analysis.apply":
        return client.post("/api/v1/rb-djay-sync/analysis/apply", json=live_apply)
    if surface_id == "http.rb_djay_sync.ratings.apply":
        return client.post("/api/v1/rb-djay-sync/ratings/apply", json=live_apply)
    if surface_id == "http.rb_djay_sync.cues.apply":
        return client.post(
            "/api/v1/rb-djay-sync/cues/apply",
            json={**live_apply, "cautious": True},
        )
    raise AssertionError(f"no HTTP probe wired for {surface_id}")


@pytest.mark.parametrize("surface", HTTP_SURFACES, ids=lambda s: s.surface_id)
def test_http_surface_refuses_while_the_gate_is_off(
    surface, client: TestClient, tmp_path: Path
) -> None:
    response = _http_probe(surface.surface_id, client, "pl-1", tmp_path)
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


def _never_called_factory(*_args, **_kwargs):
    raise AssertionError("the gate must refuse before any writer is opened")


def _probe_playlist_writeback_apply(tmp: Path) -> None:
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
        target=module.WritebackTarget("live", str(tmp / "nope.db"), "t-1"),
        plan_token="tok", dry_run=False, confirmed=True,
    )


def _probe_smartlists_rb_writer(tmp: Path) -> None:
    from apps.smartlists.rb_writer import RBPlaylistWriter

    writer = RBPlaylistWriter(
        db=object(), state_conn=sqlite3.connect(":memory:"), live=True,
        live_db_path=tmp / "master.db",
    )
    writer._assert_safe_to_write()


def _probe_relocate_write_folder_path(tmp: Path) -> None:
    from apps.webui.server.routes.relocate import _write_rekordbox_folder_path

    _write_rekordbox_folder_path("9", str(tmp / "gone.mp3"), object(), "identity")


def _probe_usb_pioneer_cli_write(tmp: Path) -> None:
    from apps.sync.usb.pioneer.__main__ import _cmd_write

    _cmd_write(SimpleNamespace(
        playlist=[], track=[], template=str(tmp / "t.db"), output=str(tmp / "o.db"),
        apply=True,
    ))


def _probe_usb_pioneer_agent_export(tmp: Path) -> None:
    from apps.sync.usb.pioneer.__main__ import _cmd_agent_export

    _cmd_agent_export(SimpleNamespace(
        playlist="Warmup", usb=str(tmp / "volume"), live=False, max_steps=1, verbose=False,
    ))


def _probe_sync_rb_writer_write_cues(_tmp: Path) -> None:
    """The target is the HANDLE, so the probe hands it one and expects a refusal.

    A `db` that raises on any attribute use proves the guard fired before the
    writer touched it. There is no path to build here at all, which is exactly
    why neither the marker sweep nor the constructor sweep could see this one.
    """
    from apps.sync.rb_writer import write_cues

    class _Explodes:
        def __getattr__(self, name: str):
            raise AssertionError(f"write_cues reached db.{name} past the gate")

    write_cues(_Explodes(), "1", [])


def _probe_dedup_apply(tmp: Path) -> None:
    """--rb-db is an argument, so the probe must not rely on a path constant."""
    from apps.dedup.apply import run_apply

    from apps.dedup.apply import DedupApplyPaths

    run_apply(apply_paths=DedupApplyPaths(rb_db_path=tmp / "master.db"), live=True)


MODULE_PROBES: dict[str, Callable[[Path], None]] = {
    "module.relocate.write_folder_path": _probe_relocate_write_folder_path,
    "module.playlist_writeback.service_apply": _probe_playlist_writeback_apply,
    "module.smartlists.rb_writer": _probe_smartlists_rb_writer,
    "module.sync.rb_writer.write_cues": _probe_sync_rb_writer_write_cues,
    "module.dedup.apply": _probe_dedup_apply,
    "module.sync.apply_ratings": lambda _tmp: __import__(
        "apps.sync.apply_ratings", fromlist=["_live_rb_db_path"]
    )._live_rb_db_path(True),
    # Write-back live (no --diff-csv) uses the same opener as CSV --live;
    # fixed-tempo PQTZ writes run inside that session too.
    "module.sync.apply_analysis": lambda _tmp: __import__(
        "apps.sync.apply_analysis", fromlist=["_live_rb_db_path"]
    )._live_rb_db_path(True),
    "module.sync.apply_cues": lambda _tmp: __import__(
        "apps.sync.apply_cues", fromlist=["live_run"]
    ).live_run([{"rb_content_id": "1"}], flag_ok=True, cautious=True),
    "module.sync.safety.live_write_session": lambda tmp: __import__(
        "apps.sync.safety", fromlist=["LiveWriteSession"]
    ).LiveWriteSession(
        target="rekordbox", reason="probe", flag_ok=True, db_path=tmp / "master.db",
    ).__enter__(),
    "module.reconcile.apply": lambda _tmp: __import__(
        "apps.reconcile.apply", fromlist=["_open_live_db"]
    )._open_live_db(),
    # The OVERRIDE, not None. Probing with None only exercises the constant
    # branch, and the --db branch returned before ever reaching the guard: a
    # fully working live-write lane behind a green test.
    "module.reconcile.remove_track": lambda tmp: __import__(
        "apps.reconcile.remove_track", fromlist=["_resolve_db_path"]
    )._resolve_db_path(tmp / "master.db", live=True),
    "module.reconcile.prefix_dead_playlists": lambda _tmp: __import__(
        "apps.reconcile.prefix_dead_playlists", fromlist=["_apply"]
    )._apply([]),
    "module.sync.usb.pioneer.export_workflow": lambda _tmp: __import__(
        "apps.sync.usb.pioneer.export_workflow", fromlist=["apply_export"]
    ).apply_export(object(), confirmation="p-1"),
    "module.sync.usb.apply": lambda tmp: __import__(
        "apps.sync.usb.apply", fromlist=["main"]
    ).main(["--profile", str(tmp / "profile.yaml"), "--cautious"]),
    "module.sync.usb.pioneer.cli_write": _probe_usb_pioneer_cli_write,
    "module.sync.usb.pioneer.agent_export": _probe_usb_pioneer_agent_export,
}


def test_every_module_surface_has_a_probe() -> None:
    """A mapped module surface with no probe is an untested gate."""
    assert set(MODULE_PROBES) == {surface.surface_id for surface in MODULE_SURFACES}


@pytest.mark.parametrize("surface", MODULE_SURFACES, ids=lambda s: s.surface_id)
def test_module_surface_refuses_while_the_gate_is_off(surface, tmp_path: Path) -> None:
    with pytest.raises(RekordboxWritebackDisabled) as caught:
        MODULE_PROBES[surface.surface_id](tmp_path)
    assert caught.value.surface_id == surface.surface_id
    assert caught.value.code == WRITEBACK_DISABLED_CODE
    assert caught.value.to_dict()["surface"] == surface.surface_id


# ----- the gate is not path-shaped, so aliases cannot slip past it ----------


def _db_override_forms(tmp_path: Path) -> dict[str, Path]:
    """The same target written three ways, all under tmp_path.

    ``Path.__eq__`` and ``Path.is_relative_to`` are LEXICAL: neither collapses
    ``..`` nor follows a symlink. Any gate written as "is this path the live
    DB?" would answer no to two of these three and let the write through. This
    gate is written on the SURFACE instead, so all three refuse identically --
    these probes are what pins that property in place.
    """
    protected = tmp_path / "rekordbox" / "master.db"
    protected.parent.mkdir(parents=True, exist_ok=True)
    protected.write_bytes(b"")
    alias = tmp_path / "rekordbox" / ".." / "rekordbox" / "master.db"
    link = tmp_path / "link-to-master.db"
    link.symlink_to(protected)
    return {"straight": protected, "dotdot-alias": alias, "symlink": link}


@pytest.mark.parametrize("form", ["straight", "dotdot-alias", "symlink"])
def test_remove_track_refuses_every_alias_of_a_db_override(
    tmp_path: Path, form: str
) -> None:
    """``--db <..-alias>`` and ``--db <symlink>`` refuse exactly like the plain path."""
    from apps.reconcile.remove_track import _resolve_db_path

    with pytest.raises(RekordboxWritebackDisabled) as caught:
        _resolve_db_path(_db_override_forms(tmp_path)[form], live=True)
    assert caught.value.surface_id == "module.reconcile.remove_track"


def test_remove_track_refuses_both_the_constant_and_the_db_override(
    tmp_path: Path,
) -> None:
    """``--db`` accepts any path and _open_db unlocks an encrypted file, so the
    override branch is a live-write lane in its own right. ``None`` exercises
    the constant branch, which is the only one the original probe covered."""
    from apps.reconcile.remove_track import _resolve_db_path

    for override in (None, tmp_path / "master.db"):
        with pytest.raises(RekordboxWritebackDisabled) as caught:
            _resolve_db_path(override, live=True)
        assert caught.value.surface_id == "module.reconcile.remove_track"


def test_remove_track_dry_run_with_an_override_still_reads(tmp_path: Path) -> None:
    """Import direction is untouched: a dry run against a fixture still resolves."""
    from apps.reconcile.remove_track import _resolve_db_path

    fixture = tmp_path / "plain.db"
    assert _resolve_db_path(fixture, live=False) == fixture
