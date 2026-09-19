"""Minimal xAI (Grok) JSON helper for settings AI search/apply.

Uses urllib (runtime dep) - same pattern as cloud_sync / voice. Fail-loud
when XAI_API_KEY is missing. Env:

  XAI_API_KEY   (required)
  XAI_BASE_URL  (default https://api.x.ai/v1)
  XAI_MODEL     (default grok-4-1-fast-non-reasoning)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class XaiConfig:
    api_key: str
    base_url: str
    model: str
    timeout_s: float = 20.0


UrlOpen = Callable[[urllib.request.Request, float], Any]


def load_xai_config() -> XaiConfig:
    key = (os.environ.get("XAI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "XAI_API_KEY is not set - settings AI search/apply require a key"
        )
    base = (os.environ.get("XAI_BASE_URL") or "https://api.x.ai/v1").rstrip("/")
    model = (os.environ.get("XAI_MODEL") or "grok-4-1-fast-non-reasoning").strip()
    return XaiConfig(api_key=key, base_url=base, model=model)


def chat_json(
    *,
    system: str,
    user: str,
    config: XaiConfig | None = None,
    urlopen: UrlOpen | None = None,
) -> tuple[dict[str, Any], str]:
    """POST /chat/completions and parse the assistant content as JSON object.

    Returns (parsed_json, model_used).
    """
    cfg = config or load_xai_config()
    opener = urlopen or (
        lambda req, timeout: urllib.request.urlopen(req, timeout=timeout)
    )
    payload = {
        "model": cfg.model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{cfg.base_url}/chat/completions",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with opener(req, cfg.timeout_s) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"xAI HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"xAI network error: {exc.reason}") from exc

    envelope = json.loads(raw)
    choices = envelope.get("choices") or []
    if not choices:
        raise RuntimeError("xAI response missing choices")
    content = choices[0].get("message", {}).get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("xAI response missing message content")
    # Models sometimes wrap JSON in fences; strip lightly.
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise RuntimeError("xAI JSON content must be an object")
    return parsed, str(envelope.get("model") or cfg.model)


__all__ = ["XaiConfig", "chat_json", "load_xai_config"]
