"""Re-export the webui daemon fixtures so emit-point tests drive real routes.

The emit points live in the legacy routers, so proving them needs the real
app + seeded backend, not a second copy of either. pytest cannot reach a
sibling directory's conftest, and ``pytest_plugins`` is rootdir-only, so the
fixtures are imported here by name -- the ONE supported way to share them.
"""
from __future__ import annotations

from typing import Literal

from apps.engine_core.build_info import BuildIdentity, BuildInfoOut
from tests.webui.conftest import (  # noqa: F401  (re-exported as fixtures)
    client,
    seed_backend,
)


def build_identity(source: Literal["payload", "repo"]) -> BuildIdentity:
    """A resolved build identity, as ``add_build_info_route`` mounts one.

    Any test app that includes the setup router needs this on ``app.state``:
    the wizard gate is derived from the build source and the router refuses
    rather than guessing one. ``payload`` is the installed dmg, ``repo`` a
    developer checkout.

    Built in full rather than faked down to the one field under test -- a
    partial identity would pass here and fail the moment anything read
    another field off it.
    """
    return BuildIdentity(
        info=BuildInfoOut(
            source=source,
            engine_version="0.1.0",
            git_sha="0d41a28c",
            git_sha_full="0d41a28c0000000000000000000000000000beef",
            git_branch="af--setup-first-run",
            git_dirty=False,
            built_at_utc="2026-08-19T12:00:00Z",
            built_at_kind=(
                "payload-build" if source == "payload" else "head-commit"
            ),
        ),
        failure=None,
    )
