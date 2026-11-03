"""Where the assistant's model calls go: the user's own key, or our gateway.

Two modes, chosen by which environment is set, never by trying one and
dropping to the other:

- ``byok``: ``OPENROUTER_API_KEY`` set. Calls go to ``MDT_OPENROUTER_BASE_URL``
  (default OpenRouter) with that key, exactly as ``/chat`` always has.
- ``gateway``: ``MDT_ASSISTANT_GATEWAY_URL`` and ``MDT_ASSISTANT_INSTALL_TOKEN``
  set. Calls go to the Open DJ Cloudflare Worker (apps/ai_gateway_worker),
  which holds the provider key and meters per install. The token is the only
  credential on this machine.

Both configured is refused (``assistant_upstream_ambiguous``) rather than
silently preferring one, because the two bill different people. Neither
configured is the existing 409 ``assistant_key_missing``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

# ----- configuration ------------------------------------------------------
OPENROUTER_KEY_ENV: str = "OPENROUTER_API_KEY"
OPENROUTER_BASE_ENV: str = "MDT_OPENROUTER_BASE_URL"
GATEWAY_URL_ENV: str = "MDT_ASSISTANT_GATEWAY_URL"
INSTALL_TOKEN_ENV: str = "MDT_ASSISTANT_INSTALL_TOKEN"

DEFAULT_OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"

KEY_MISSING_CODE: str = "assistant_key_missing"
AMBIGUOUS_CODE: str = "assistant_upstream_ambiguous"
TOKEN_MISSING_CODE: str = "assistant_install_token_missing"

UpstreamMode = Literal["byok", "gateway"]


@dataclass(frozen=True)
class Upstream:
    """An OpenAI-compatible ``/chat/completions`` base and its credential."""

    mode: UpstreamMode
    #: No trailing slash; callers append ``/chat/completions``.
    base_url: str
    #: The bearer value: the OpenRouter key (byok) or the install token.
    bearer: str

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.bearer}",
            # OpenRouter's app-attribution headers; the Worker forwards them.
            "X-Title": "Open DJ",
            "HTTP-Referer": "https://github.com/private_owner/music-dj-tools",
        }


class UpstreamUnconfigured(Exception):
    """No usable upstream. ``code`` is the refusal code the route returns."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ----- helpers ------------------------------------------------------------
def _env(name: str) -> str | None:
    """The value, or None. Empty/whitespace counts as absent (``KEY=`` clears)."""
    return os.environ.get(name, "").strip() or None


def resolve_upstream() -> Upstream:
    """The configured upstream, or :class:`UpstreamUnconfigured`."""
    key = _env(OPENROUTER_KEY_ENV)
    gateway = _env(GATEWAY_URL_ENV)
    token = _env(INSTALL_TOKEN_ENV)
    if key and gateway:
        raise UpstreamUnconfigured(
            AMBIGUOUS_CODE,
            f"both {OPENROUTER_KEY_ENV} and {GATEWAY_URL_ENV} are set; they bill "
            "different accounts, so unset one and restart the engine.",
        )
    if gateway:
        if not token:
            raise UpstreamUnconfigured(
                TOKEN_MISSING_CODE,
                f"{GATEWAY_URL_ENV} is set but {INSTALL_TOKEN_ENV} is not; the "
                "gateway refuses calls without an install token.",
            )
        return Upstream(mode="gateway", base_url=gateway.rstrip("/"), bearer=token)
    if key:
        base = _env(OPENROUTER_BASE_ENV) or DEFAULT_OPENROUTER_BASE_URL
        return Upstream(mode="byok", base_url=base.rstrip("/"), bearer=key)
    raise UpstreamUnconfigured(
        KEY_MISSING_CODE,
        f"the assistant has no upstream: set {OPENROUTER_KEY_ENV}, or "
        f"{GATEWAY_URL_ENV} with {INSTALL_TOKEN_ENV}, in the engine's environment "
        "and restart it. Nothing is answered without one.",
    )
