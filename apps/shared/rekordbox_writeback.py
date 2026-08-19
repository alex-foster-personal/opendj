"""ONE-WAY IMPORT GATE in front of real rekordbox data.

rekordbox -> app (import, decrypt, read, snapshot into ``data/``) is allowed
and unchanged.  app -> rekordbox (any write that could reach the real library)
is refused unless an operator explicitly turns this gate on.  The code stays in
the tree, compiles, and is tested; it is disabled, not deleted.

WHAT COUNTS AS "REAL REKORDBOX DATA"
------------------------------------
GATED (a bug here can damage the user's library):

  * ``~/Library/Pioneer/rekordbox/master.db`` -- the LIVE rekordbox database
    (``apps.shared.paths.REKORDBOX_LIVE_DB``).
  * the Pioneer share directory (``apps.shared.platform_paths.SHARE_ROOT``)
    when written INTO rather than read from.
  * a mounted USB device export (``PIONEER/rekordbox/exportLibrary.db`` and
    the mirrored audio tree) -- CDJ media the user actually plays from.

NOT GATED (the app's own working copies; losing them costs a re-import):

  * ``data/master.plain.db`` -- decrypted working copy.  ``rb_vendor`` and the
    hot-cue routes write here on purpose.
  * ``data/master.db.copy`` (``REKORDBOX_WORKING_DB``), ``data/state/state.db``,
    ``data/writeback-backups/``, ``data/reconcile/backups/``, the anlz cache.
  * djay Pro's ``MediaLibrary.db`` and audio-file ID3 tags -- different vendors,
    a different decision, out of scope for THIS gate.

THE MAP
-------
Every entry in :data:`WRITE_SURFACES` is a path that writes toward the gated
set above.  ``tests/test_rekordbox_writeback_gate.py`` iterates it, so:

  * a mapped surface that stops calling the guard fails the static test;
  * a newly added live-rekordbox write path that is not mapped fails the
    unmapped-write-path sweep;
  * :func:`require_writeback_enabled` rejects an unregistered ``surface_id``,
    so the guard cannot be called from a path nobody wrote down.

  surface_id                                  what it would touch
  ------------------------------------------- -------------------------------
  http.playlists.writeback.apply              live master.db playlist members
  http.playlists.writeback.rollback           live master.db (restore backup)
  http.relocate.apply                         live master.db djmdContent.FolderPath
  http.usb-export.apply                       USB volume exportLibrary.db
  module.relocate.write_folder_path           live master.db FolderPath patch
  module.playlist_writeback.service_apply     live master.db (service layer)
  module.playlist_writeback.service_rollback  live master.db (service layer)
  module.smartlists.rb_writer                 live master.db playlists
  module.sync.apply_ratings                   live master.db ratings
  module.sync.apply_analysis                  live master.db bpm/key
  module.sync.apply_cues                      live master.db djmdCue
  module.sync.safety.live_write_session       live master.db (backstop rail)
  module.reconcile.apply                      live master.db rows
  module.reconcile.remove_track               live master.db row deletes
  module.reconcile.prefix_dead_playlists      live master.db playlist renames
  module.sync.usb.pioneer.export_workflow     USB volume exportLibrary.db
  module.sync.usb.apply                       USB volume audio mirror
  module.sync.usb.pioneer.cli_write           exportLibrary.db at any --output
  module.sync.usb.pioneer.agent_export        the real rekordbox GUI, clicked

The USB sub-modules (``usb/copy.py``, ``usb/playlist_writer.py``,
``usb/marker.py``, ``usb/pioneer/writer_rbox.py``) are reachable only through
the mapped entrypoints above, so they are covered transitively rather than
guarded twice.  A NEW entrypoint into any of them must be added to this map.

DELIBERATE OVER-BLOCK
---------------------
``POST /api/v1/relocate/{stable_id}/apply`` is gated whole, not just its
rekordbox branch.  The route picks its branch at runtime from the track's
vendor mapping, so a caller cannot know in advance whether a given relocate
writes the live master.db or only state.db.  Refusing the whole route keeps
"the control is visibly off" true, at the cost of also blocking relocates for
the minority of tracks with no rekordbox mapping.  The narrower guard inside
``_write_rekordbox_folder_path`` is kept as defence in depth so a future
refactor that moves that write off the route still hits the gate.

FLAG SEMANTICS
--------------
``MDT_REKORDBOX_WRITEBACK_ENABLED``, unset or empty -> OFF.  ``1/true/yes/on``
-> ON.  ``0/false/no/off`` -> OFF.  Anything else raises: a typo must not be
read as "off" by luck, and must never be read as "on".

A gated call refuses with ONE distinct error, :class:`RekordboxWritebackDisabled`
(code ``rekordbox_writeback_disabled``).  Never a silent no-op and never a faked
success -- the caller is told exactly why nothing happened.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

REKORDBOX_WRITEBACK_ENABLED_ENV = "MDT_REKORDBOX_WRITEBACK_ENABLED"

#: The one error code every refusal carries, on the wire and in the exception.
WRITEBACK_DISABLED_CODE = "rekordbox_writeback_disabled"

WRITEBACK_DISABLED_MESSAGE = (
    "sync to rekordbox is disabled: this build runs in one-way import mode "
    "(rekordbox -> app only). No write toward the real rekordbox library, "
    "share directory, or USB export can run. Set "
    f"{REKORDBOX_WRITEBACK_ENABLED_ENV}=1 to allow it."
)

#: The exact tooltip every disabled UI control carries. Deliberately NOT the
#: "not implemented - see PARITY-TODO" wording: these features ARE built and
#: work, they are switched off on purpose, which is a different fact.
UI_REFUSAL_TITLE = "sync to rekordbox disabled - one-way import only"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSEY = frozenset({"", "0", "false", "no", "off"})

SurfaceKind = Literal["http", "module"]


@dataclass(frozen=True)
class WriteSurface:
    """One code path that writes toward real rekordbox data.

    ``guard_site`` is the repo-relative file that must contain
    ``require_writeback_enabled("<surface_id>")``.  The static half of the
    gate test reads that file and checks for exactly that literal, so moving
    a guard without updating the map is a test failure rather than a silent
    hole.
    """

    surface_id: str
    kind: SurfaceKind
    entrypoint: str
    target: str
    guard_site: str


WRITE_SURFACES: tuple[WriteSurface, ...] = (
    # ----- HTTP (agent-native parity: each has a UI control behind it) -----
    WriteSurface(
        surface_id="http.playlists.writeback.apply",
        kind="http",
        entrypoint="POST /api/v1/playlists/{playlist_id}/writeback/apply",
        target="live master.db playlist membership",
        guard_site="apps/webui/server/routes/playlist_writeback.py",
    ),
    WriteSurface(
        surface_id="http.playlists.writeback.rollback",
        kind="http",
        entrypoint="POST /api/v1/playlists/{playlist_id}/writeback/rollback",
        target="live master.db (restores a writeback backup into it)",
        guard_site="apps/webui/server/routes/playlist_writeback.py",
    ),
    WriteSurface(
        surface_id="http.relocate.apply",
        kind="http",
        entrypoint="POST /api/v1/relocate/{stable_id}/apply",
        target="live master.db djmdContent.FolderPath",
        guard_site="apps/webui/server/routes/relocate.py",
    ),
    WriteSurface(
        surface_id="http.usb-export.apply",
        kind="http",
        entrypoint="POST /api/v1/usb-export/apply",
        target="USB volume PIONEER/rekordbox/exportLibrary.db",
        guard_site="apps/webui/server/routes/usb_export.py",
    ),
    # ----- module / CLI ---------------------------------------------------
    WriteSurface(
        surface_id="module.relocate.write_folder_path",
        kind="module",
        entrypoint="apps.webui.server.routes.relocate._write_rekordbox_folder_path",
        target="live master.db djmdContent.FolderPath (defence in depth)",
        guard_site="apps/webui/server/routes/relocate.py",
    ),
    WriteSurface(
        surface_id="module.playlist_writeback.service_apply",
        kind="module",
        entrypoint="apps.webui.server.playlist_writeback.WritebackService.apply",
        target="live master.db (service layer, past dry-run and confirm)",
        guard_site="apps/webui/server/playlist_writeback.py",
    ),
    WriteSurface(
        surface_id="module.playlist_writeback.service_rollback",
        kind="module",
        entrypoint="apps.webui.server.playlist_writeback.WritebackService.rollback",
        target="live master.db (service layer, restores a backup)",
        guard_site="apps/webui/server/playlist_writeback.py",
    ),
    WriteSurface(
        surface_id="module.smartlists.rb_writer",
        kind="module",
        entrypoint="apps.smartlists.rb_writer.RBPlaylistWriter._assert_safe_to_write",
        target="live master.db playlists (create / diff / restore)",
        guard_site="apps/smartlists/rb_writer.py",
    ),
    WriteSurface(
        surface_id="module.sync.apply_ratings",
        kind="module",
        entrypoint="apps.sync.apply_ratings._live_rb_db_path(live=True)",
        target="live master.db DjmdContent ratings",
        guard_site="apps/sync/apply_ratings.py",
    ),
    WriteSurface(
        surface_id="module.sync.apply_analysis",
        kind="module",
        entrypoint="apps.sync.apply_analysis._live_rb_db_path(live=True)",
        target="live master.db bpm / key analysis",
        guard_site="apps/sync/apply_analysis.py",
    ),
    WriteSurface(
        surface_id="module.sync.apply_cues",
        kind="module",
        entrypoint="apps.sync.apply_cues.live_run",
        target="live master.db DjmdCue",
        guard_site="apps/sync/apply_cues.py",
    ),
    WriteSurface(
        surface_id="module.sync.safety.live_write_session",
        kind="module",
        entrypoint='apps.sync.safety.LiveWriteSession(target="rekordbox")',
        target="live master.db (backstop under every seven-rail live write)",
        guard_site="apps/sync/safety.py",
    ),
    WriteSurface(
        surface_id="module.reconcile.apply",
        kind="module",
        entrypoint="apps.reconcile.apply._open_live_db",
        target="live master.db rows (broken-track reconcile)",
        guard_site="apps/reconcile/apply.py",
    ),
    WriteSurface(
        surface_id="module.reconcile.remove_track",
        kind="module",
        entrypoint="apps.reconcile.remove_track._resolve_db_path(live=True)",
        target="live master.db row deletes plus cascade dependents",
        guard_site="apps/reconcile/remove_track.py",
    ),
    WriteSurface(
        surface_id="module.reconcile.prefix_dead_playlists",
        kind="module",
        entrypoint="apps.reconcile.prefix_dead_playlists._apply",
        target="live master.db playlist renames",
        guard_site="apps/reconcile/prefix_dead_playlists.py",
    ),
    WriteSurface(
        surface_id="module.sync.usb.pioneer.export_workflow",
        kind="module",
        entrypoint="apps.sync.usb.pioneer.export_workflow.apply_export",
        target="USB volume PIONEER/rekordbox/exportLibrary.db",
        guard_site="apps/sync/usb/pioneer/export_workflow.py",
    ),
    WriteSurface(
        surface_id="module.sync.usb.apply",
        kind="module",
        entrypoint="apps.sync.usb.apply.main",
        target="USB volume audio mirror plus playlist files",
        guard_site="apps/sync/usb/apply.py",
    ),
    WriteSurface(
        surface_id="module.sync.usb.pioneer.cli_write",
        kind="module",
        entrypoint="python -m apps.sync.usb.pioneer write --apply",
        target="an exportLibrary.db written to any --output path, USB included",
        guard_site="apps/sync/usb/pioneer/__main__.py",
    ),
    WriteSurface(
        surface_id="module.sync.usb.pioneer.agent_export",
        kind="module",
        entrypoint="python -m apps.sync.usb.pioneer agent-export",
        target="the REAL rekordbox GUI, driven by synthetic clicks",
        guard_site="apps/sync/usb/pioneer/__main__.py",
    ),
)

SURFACES_BY_ID: dict[str, WriteSurface] = {
    surface.surface_id: surface for surface in WRITE_SURFACES
}


class RekordboxWritebackDisabled(RuntimeError):
    """One-way import mode refused a write toward real rekordbox data."""

    code: str = WRITEBACK_DISABLED_CODE

    def __init__(self, surface_id: str) -> None:
        self.surface_id = surface_id
        self.message = WRITEBACK_DISABLED_MESSAGE
        super().__init__(f"{WRITEBACK_DISABLED_MESSAGE} (surface: {surface_id})")

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "surface": self.surface_id,
        }


def writeback_enabled() -> bool:
    """Read the gate. Default OFF; an unrecognised value raises.

    Fail fast rather than guess: ``MDT_REKORDBOX_WRITEBACK_ENABLED=ture`` must
    not be silently read as "off" (the operator thinks writes are on) and must
    never be read as "on".
    """
    raw = os.environ.get(REKORDBOX_WRITEBACK_ENABLED_ENV, "").strip().lower()
    if raw in _FALSEY:
        return False
    if raw in _TRUTHY:
        return True
    raise ValueError(
        f"{REKORDBOX_WRITEBACK_ENABLED_ENV}={raw!r} is not a boolean; use "
        f"one of {sorted(_TRUTHY)} or {sorted(_FALSEY - {''})}"
    )


def require_writeback_enabled(surface_id: str) -> None:
    """Refuse a write toward real rekordbox data while the gate is off.

    ``surface_id`` MUST already be in :data:`WRITE_SURFACES`; an unknown id
    raises ``KeyError`` so a new write path cannot quietly guard itself
    without being written into the map the review gate rests on.
    """
    if surface_id not in SURFACES_BY_ID:
        raise KeyError(
            f"unmapped rekordbox write surface {surface_id!r}: add a WriteSurface "
            "to apps/shared/rekordbox_writeback.WRITE_SURFACES before guarding it"
        )
    if writeback_enabled():
        return
    raise RekordboxWritebackDisabled(surface_id)


__all__ = [
    "REKORDBOX_WRITEBACK_ENABLED_ENV",
    "SURFACES_BY_ID",
    "UI_REFUSAL_TITLE",
    "WRITEBACK_DISABLED_CODE",
    "WRITEBACK_DISABLED_MESSAGE",
    "WRITE_SURFACES",
    "RekordboxWritebackDisabled",
    "WriteSurface",
    "require_writeback_enabled",
    "writeback_enabled",
]
