"""Which environment variables name openDJ's Google OAuth client id.

One home for the names, below both of their readers:

* :mod:`apps.webui.server.auth` reads the id to run sign-in;
* :mod:`apps.sync_hub.google_id_token` reads it to pin ``aud`` when a spoke
  presents a Google id_token at enrollment (ADR 12 section B).

It lives in ``apps.shared`` because the hub cannot import the webui:
``apps.webui`` already imports ``apps.sync_hub``, so that edge would be a
package cycle. Two copies of the list is how sign-in and verification start
disagreeing about which OAuth client this install is, and a hub that pinned
``aud`` to a different client than the one its installs sign in with would
refuse every real user while looking correctly configured.
"""

from __future__ import annotations

from collections.abc import Mapping

# Precedence is explicit, not a hidden default: an openDJ-specific client
# wins if one is ever provisioned, otherwise the shared personal client in
# Doppler (project ``general``, config ``dev_personal``) is used.
CLIENT_ID_ENV_NAMES: tuple[str, ...] = (
    "OPENDJ_GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_ID",
)


def first_present(env: Mapping[str, str], names: tuple[str, ...]) -> str | None:
    """The first non-blank value among ``names`` in ``env``, or ``None``."""
    for name in names:
        value = env.get(name, "").strip()
        if value:
            return value
    return None


__all__ = ["CLIENT_ID_ENV_NAMES", "first_present"]
