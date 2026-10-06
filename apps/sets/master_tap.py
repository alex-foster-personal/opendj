"""How the daemon gets a page to tap its master bus on a master start (SET-12).

A ``source: master`` recording is fed by the open /performance page. Waiting
for the page to notice on its own (its REC rail's status poll) cost an
agent-started recording its first 4.1-4.6 s (CORE, build 15). So the start
route PUSHES the attach to the page and returns only once the tap is
connected; the WAV then begins within one order round trip of the start.

The push channel is the host app's business: the webui app installs one that
rides the AGENT-03 order bus (``apps.webui.server.sets_master_tap``) on
``app.state.sets_master_tap_channel``. This module only names the contract,
so ``apps.sets`` does not import the web server.
"""
from __future__ import annotations

from typing import Protocol

from fastapi import Request

#: ``app.state`` attribute holding the host's :class:`MasterTapChannel`.
MASTER_TAP_CHANNEL_STATE = "sets_master_tap_channel"


class MasterTapUnavailable(RuntimeError):
    """No page can tap the master mix now; the message says why."""


class MasterTapChannel(Protocol):
    def unavailable_reason(self, request: Request) -> str | None:
        """Why no page could be asked to tap, checked BEFORE a recording starts."""
        ...

    async def attach(self, request: Request, session_id: str) -> None:
        """Have the page tap its master bus into ``session_id``; return once the
        tap is connected, or raise :class:`MasterTapUnavailable`."""
        ...


def master_tap_channel(request: Request) -> MasterTapChannel:
    """The host's channel, or :class:`MasterTapUnavailable` when it has none."""
    channel = getattr(request.app.state, MASTER_TAP_CHANNEL_STATE, None)
    if channel is None:
        raise MasterTapUnavailable(
            "this server has no channel to a /performance page, so it cannot record the master mix"
        )
    return channel


__all__ = [
    "MASTER_TAP_CHANNEL_STATE",
    "MasterTapChannel",
    "MasterTapUnavailable",
    "master_tap_channel",
]
