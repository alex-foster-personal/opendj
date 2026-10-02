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
    hot-cue routes write here on purpose.  ``python -m apps.sync.apply_analysis``
    without ``--live`` opens that file ``mode=ro`` and is not a write surface;
    the gated surface ``module.sync.apply_analysis`` remains the ``--live`` path
    through ``_live_rb_db_path(live=True)``.
  * ``data/master.db.copy`` (``REKORDBOX_WORKING_DB``), ``data/state/state.db``,
    ``data/writeback-backups/``, ``data/reconcile/backups/``, the anlz cache.
  * djay Pro's ``MediaLibrary.db`` and audio-file ID3 tags -- different vendors,
    a different decision, out of scope for THIS gate.

THE MAP
-------
:data:`WRITE_SURFACES` is the complete INVENTORY of paths that write toward the
gated set above.  Most carry ``gated=True`` and refuse while the flag is off; a
few carry ``gated=False`` with a written reason (see RECOVERY PATHS below) and
are enumerated anyway, so the map stays the whole truth rather than only the
blocked half.  ``tests/test_rekordbox_writeback_gate.py`` iterates it, so:

  * a gated surface that stops calling the guard fails the static test;
  * an UNgated surface that quietly grows a guard also fails it, so the
    deliberate exception cannot be reverted by accident;
  * a newly added live-rekordbox write path that is not mapped fails both the
    live-target marker sweep and the constructor-provenance sweep;
  * :func:`require_writeback_enabled` rejects an unregistered ``surface_id``
    AND an ``surface_id`` that is mapped as ungated, so the guard cannot be
    called from a path nobody wrote down.

  surface_id                                  what it would touch
  ------------------------------------------- -------------------------------
  http.playlists.writeback.apply              live master.db playlist members
  http.playlists.writeback.rollback           live master.db  [UNGATED]
  http.relocate.apply                         live master.db djmdContent.FolderPath
  http.usb-export.apply                       USB volume exportLibrary.db
  module.relocate.write_folder_path           live master.db FolderPath patch
  module.playlist_writeback.service_apply     live master.db (service layer)
  module.playlist_writeback.service_rollback  live master.db  [UNGATED]
  module.smartlists.rb_writer                 live master.db playlists
  module.sync.rb_writer.write_cues            live master.db djmdCue, on an
                                              INJECTED db handle (no path)
  module.sync.apply_ratings                   live master.db ratings
  module.sync.apply_analysis                  live master.db bpm/key/loudness
                                              and fixed-tempo ANLZ PQTZ
                                              (write-back and CSV bpm/energy)
  module.sync.apply_cues                      live master.db djmdCue
  module.sync.safety.live_write_session       live master.db (backstop rail)
  module.reconcile.apply                      live master.db rows
  module.reconcile.remove_track               live master.db row deletes
  module.reconcile.prefix_dead_playlists      live master.db playlist renames
  module.dedup.apply                          live master.db FolderPath rewrite
                                              plus a raw copy2 restore
  module.sync.usb.pioneer.export_workflow     USB volume exportLibrary.db
  module.sync.usb.apply                       USB volume audio mirror
  module.sync.usb.pioneer.cli_write           exportLibrary.db at any --output
  module.sync.usb.pioneer.agent_export        the real rekordbox GUI, clicked

The USB sub-modules (``usb/copy.py``, ``usb/playlist_writer.py``,
``usb/marker.py``, ``usb/pioneer/writer_onelibrary.py``) are reachable only through
the mapped entrypoints above, so they are covered transitively rather than
guarded twice.  A NEW entrypoint into any of them must be added to this map.

PATH-BY-ARGUMENT AND HANDLE-BY-ARGUMENT
---------------------------------------
A write surface does not have to NAME its target.  Three shapes exist and each
needs its guard in a different place:

  * path from a CONSTANT (``paths.REKORDBOX_LIVE_DB``) -- guard where the
    constant is resolved;
  * path from an ARGUMENT or CLI flag (``--db``, ``--rb-db``) -- guard the
    argument branch too, not only the constant branch.  ``remove_track``
    returned its ``--db`` override BEFORE reaching the guard, which is how a
    fully working live-write lane sat behind a green suite;
  * an already-open DB HANDLE passed in (``write_cues(db, ...)``) -- there is
    no path to guard at all, so the guard goes INSIDE the function.

None of these three name a live path, so a grep for path constants cannot see
the last two.  That is why the test suite also sweeps DB-constructor call
sites by provenance.

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

RECOVERY PATHS ARE NOT GATED
---------------------------
Rollback (``.../writeback/rollback`` and ``WritebackService.rollback``) is
enumerated with ``gated=False``.  A recovery path only exists AFTER a write
already landed, so gating it traps the user with a bad write and no undo --
strictly worse for the data this gate protects.  It is safe to leave open
because it cannot be aimed anywhere: it replays a JSON preimage that only the
GATED apply can mint (``writeback_backup.write_reversal`` binds backup_id +
vendor + target_path + target_id + post_apply_revision, and the id must match
``[0-9a-f]{32}``), the target path is pinned to the canonical live DB by
``_require_exact_live_target``, the current membership is CAS-checked before
the mutation, and the scope is one playlist.  It never restores a ``.db``
snapshot over the target.  Every non-gate rail (typed confirm, pgrep, the
exclusive lock, CAS, manifest provenance) still runs.

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

    ``gated=False`` marks a surface deliberately left open; ``reason`` then
    carries why, and the static test asserts the guard literal is ABSENT so
    the exception cannot be re-gated by a careless refactor.  An ungated
    surface stays in the map because the map is the write INVENTORY, not just
    the blocked list.
    """

    surface_id: str
    kind: SurfaceKind
    entrypoint: str
    target: str
    guard_site: str
    gated: bool = True
    reason: str = ""


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
        target="live master.db (replays the apply's own preimage into it)",
        guard_site="apps/webui/server/routes/playlist_writeback.py",
        gated=False,
        reason=(
            "recovery path: it can only exist after a GATED apply landed, and "
            "gating it would trap the user with a bad write and no undo. It "
            "cannot be aimed: the preimage is minted only by the gated apply, "
            "backup_id must match [0-9a-f]{32}, the target is pinned to the "
            "canonical live DB, and current membership is CAS-checked."
        ),
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
    WriteSurface(
        surface_id="http.rb_djay_sync.analysis.apply",
        kind="http",
        entrypoint="POST /api/v1/rb-djay-sync/analysis/apply",
        target="live master.db bpm/key/loudness and CSV analysis sync",
        guard_site="apps/sync/djay_sync_service.py",
    ),
    WriteSurface(
        surface_id="http.rb_djay_sync.ratings.apply",
        kind="http",
        entrypoint="POST /api/v1/rb-djay-sync/ratings/apply",
        target="live master.db and djay MediaLibrary.db ratings",
        guard_site="apps/sync/djay_sync_service.py",
    ),
    WriteSurface(
        surface_id="http.rb_djay_sync.cues.apply",
        kind="http",
        entrypoint="POST /api/v1/rb-djay-sync/cues/apply",
        target="live master.db djmdCue (scaffold live path only)",
        guard_site="apps/sync/djay_sync_service.py",
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
        target="live master.db (service layer, replays a preimage)",
        guard_site="apps/webui/server/playlist_writeback.py",
        gated=False,
        reason=(
            "same recovery-path reasoning as http.playlists.writeback.rollback. "
            "RBPlaylistWriter.restore_backup opts out of the gate rail only; "
            "pgrep, the exclusive lock, CAS, and manifest provenance all run."
        ),
    ),
    WriteSurface(
        surface_id="module.smartlists.rb_writer",
        kind="module",
        entrypoint="apps.smartlists.rb_writer.RBPlaylistWriter._assert_safe_to_write",
        target="live master.db playlists (create / diff / apply)",
        guard_site="apps/smartlists/rb_writer.py",
    ),
    WriteSurface(
        surface_id="module.sync.rb_writer.write_cues",
        kind="module",
        entrypoint="apps.sync.rb_writer.write_cues",
        target=(
            "DjmdCue on whatever Rekordbox6Database handle it is passed, "
            "the live one included -- no path of its own to guard"
        ),
        guard_site="apps/sync/rb_writer.py",
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
        target=(
            "live master.db bpm / key / loudness (sidecar) write-back, "
            "fixed-tempo ANLZ PQTZ .DAT, and CSV bpm/energy analysis sync"
        ),
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
        entrypoint=(
            "apps.reconcile.remove_track._resolve_db_path(live=True), BOTH the "
            "constant branch and the --db override branch"
        ),
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
        surface_id="module.dedup.apply",
        kind="module",
        entrypoint="apps.dedup.apply.run_apply(live=True) / python -m apps.dedup.apply --live",
        target=(
            "live master.db djmdContent.FolderPath rewrites at any --rb-db "
            "path, plus a raw shutil.copy2 of the backup over that file when "
            "post-write verify fails"
        ),
        guard_site="apps/dedup/apply.py",
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

    An id mapped with ``gated=False`` also raises ``KeyError``: those are
    deliberate, argued exceptions (recovery paths), and re-gating one has to
    be a decision recorded in the map, never a stray call site.
    """
    surface = SURFACES_BY_ID.get(surface_id)
    if surface is None:
        raise KeyError(
            f"unmapped rekordbox write surface {surface_id!r}: add a WriteSurface "
            "to apps/shared/rekordbox_writeback.WRITE_SURFACES before guarding it"
        )
    if not surface.gated:
        raise KeyError(
            f"rekordbox write surface {surface_id!r} is mapped as deliberately "
            f"UNGATED ({surface.reason}); flip gated=True in WRITE_SURFACES "
            "if that decision has changed"
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
