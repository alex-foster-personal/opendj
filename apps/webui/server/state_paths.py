"""The ONE function that resolves ``state.db``'s on-disk path (issue #2589).

``preflight.py`` used to read ``apps.adapters.rekordbox.config.STATE_DB`` -- a
module-level constant frozen the first time that module is imported -- while
``health.py`` read ``request.app.state.state_db_path``, the value
``_bind_core_state`` actually binds at boot and the same one every other
route in this package trusts (``tracks.py``, ``feedback_sync.py``,
``lyrics_search.py``). Nothing forced those two computations to agree: the
existing preflight test fixtures monkeypatch only ``rb_config.STATE_DB`` and
never touch ``app.state.state_db_path`` at all, so under test they already
pointed at two different files without either check knowing. In the field
this surfaced as issue #2589 -- health reported ``tracks: 0`` for a path
preflight swore did not exist, for what was supposedly the identical file.

Every consumer of "where is state.db" now calls this one function instead of
re-deriving it, so there is no second place for the two to drift apart.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Request

#: Matches ``create_app``'s own default for ``state_db_path`` (relative to
#: CWD) so a caller with no bound app.state still gets the same answer that
#: parameter would produce.
DEFAULT_STATE_DB_PATH: str = "data/state/state.db"


def resolve_state_db_path(request: Request) -> Path:
    """The on-disk ``state.db`` path this app instance was actually bound to."""
    return Path(getattr(request.app.state, "state_db_path", DEFAULT_STATE_DB_PATH))


__all__ = ["DEFAULT_STATE_DB_PATH", "resolve_state_db_path"]
