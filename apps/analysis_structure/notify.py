"""Tell a running engine that the CLI just wrote structure sidecars (issue #6496).

The All-In-One CLI runs out of process, so it cannot publish on the engine's
event hub. After writing sidecars it POSTs their ids to the engine's
local-operator route ``POST /api/v1/structure/sidecars-changed``, which emits the
same ``library.changed`` kind ``sections`` event own-fast does, so a loaded deck
refetches once. Best effort: no engine, a stale lock or any HTTP error is
reported as ``False`` and never fails the CLI.

The engine is found the way ``opendj api`` finds it: ``MDT_ENGINE_URL`` when set,
else the engine lock file (``OPENDJ_LIVE_LOCK_PATH`` override, else the app's).
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import urllib.request
from collections.abc import Mapping, Sequence

from apps.shared.engine_origin import EngineNotRunning, resolve_origin

_log = logging.getLogger(__name__)

ENGINE_URL_ENV = "MDT_ENGINE_URL"
NOTIFY_PATH = "/api/v1/structure/sidecars-changed"
NOTIFY_TIMEOUT_S = 2.0
#: Stays under the route's ``MAX_NOTIFY_IDS`` so a long ``--missing`` run still lands.
CHUNK = 500


def _post_json(url: str, body: dict[str, object]) -> None:
    """The one HTTP call: POST ``body`` as JSON, raising on any non-2xx answer.

    Proxies are bypassed: the engine is on this machine, and an
    ``HTTP_PROXY`` in the shell must not relay the call elsewhere.
    """
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=NOTIFY_TIMEOUT_S) as resp:
        resp.read()


def engine_base_url(environ: Mapping[str, str] | None = None) -> str | None:
    """The running engine's base URL, or None when none can be found."""
    env = os.environ if environ is None else environ
    override = env.get(ENGINE_URL_ENV, "").strip()
    if override:
        return override.rstrip("/")
    try:
        return resolve_origin(None, env).base_url
    except (EngineNotRunning, OSError, RuntimeError, ValueError):
        return None


def notify_sidecars_changed(stable_ids: Sequence[str], environ: Mapping[str, str] | None = None) -> bool:
    """POST the written ids to the engine; True only when every chunk was accepted."""
    ids = list(dict.fromkeys(stable_ids))
    if not ids:
        return True
    base = engine_base_url(environ)
    if base is None:
        _log.info("no running engine found; loaded decks refresh on their next load")
        return False
    for start in range(0, len(ids), CHUNK):
        try:
            _post_json(f"{base}{NOTIFY_PATH}", {"stable_ids": ids[start : start + CHUNK]})
        except (OSError, ValueError, http.client.HTTPException) as exc:  # URLError/HTTPError are OSErrors
            _log.info("engine notify at %s failed: %s", base, exc)
            return False
    return True


__all__ = ["ENGINE_URL_ENV", "NOTIFY_PATH", "engine_base_url", "notify_sidecars_changed"]
