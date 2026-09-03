"""Central configuration for META-05 streaming transfers."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class TransferConfig:
    soundcloud_api_base: str = "https://api.soundcloud.com"
    openrouter_api_base: str = "https://openrouter.ai/api/v1"
    soundcloud_token_env: str = "SOUNDCLOUD_ACCESS_TOKEN"
    openrouter_key_env: str = "OPENROUTER_API_KEY"
    openrouter_model_env: str = "MDT_STREAMING_TRANSFER_MODEL"
    search_limit: int = 5
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 180.0

    def require_secret(self, env_name: str) -> str:
        value = os.environ.get(env_name, "").strip()
        if not value:
            raise TransferConfigurationError(
                f"{env_name} is required for streaming-library transfer"
            )
        return value


class TransferConfigurationError(RuntimeError):
    """A required provider setting is absent or invalid."""


CFG = TransferConfig()
