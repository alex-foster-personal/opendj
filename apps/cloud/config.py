"""CloudConfig -- R2 endpoint + bucket + Doppler env var names.

All R2 credentials come from Doppler. Never put them in ``.env`` files.

Invocation pattern for production::

    doppler run -p music-dj-tools -c prod -- \
        python -m apps.cloud.replicate

The three required env vars are:
  * ``R2_ACCOUNT_ID``
  * ``R2_ACCESS_KEY_ID``
  * ``R2_SECRET_ACCESS_KEY``

Bucket names default to ``music-dj-state`` and ``music-dj-audio`` but can
be overridden via ``MUSIC_DJ_STATE_BUCKET`` / ``MUSIC_DJ_AUDIO_BUCKET``.
"""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass


REQUIRED_VARS: tuple[str, ...] = (
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
)


class MissingEnvError(RuntimeError):
    """Raised when a required environment variable is missing."""


@dataclass(frozen=True)
class CloudConfig:
    """Runtime config for the cloud-sync daemon.

    Bind host defaults to ``127.0.0.1`` per D5 (CAT-05b). Any override is
    surfaced to the web UI via a warning banner.
    """

    r2_account_id: str
    r2_access_key_id: str
    r2_secret_access_key: str
    state_bucket: str
    audio_bucket: str
    hostname: str
    bind_host: str

    @property
    def r2_endpoint(self) -> str:
        """S3-compatible endpoint URL used by aioboto3 / Litestream."""
        return f"https://{self.r2_account_id}.r2.cloudflarestorage.com"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "CloudConfig":
        """Load config from ``env`` (defaults to ``os.environ``).

        Raises :class:`MissingEnvError` with a clear list of missing vars
        rather than a raw ``KeyError``.
        """
        source = dict(os.environ) if env is None else dict(env)
        missing = [v for v in REQUIRED_VARS if not source.get(v)]
        if missing:
            raise MissingEnvError(
                "Missing required env vars: "
                + ", ".join(missing)
                + ". Ensure you invoked under `doppler run -- ...`."
            )
        return cls(
            r2_account_id=source["R2_ACCOUNT_ID"],
            r2_access_key_id=source["R2_ACCESS_KEY_ID"],
            r2_secret_access_key=source["R2_SECRET_ACCESS_KEY"],
            state_bucket=source.get("MUSIC_DJ_STATE_BUCKET", "music-dj-state"),
            audio_bucket=source.get("MUSIC_DJ_AUDIO_BUCKET", "music-dj-audio"),
            hostname=source.get("MUSIC_DJ_HOSTNAME", socket.gethostname()),
            bind_host=source.get("MUSIC_DJ_BIND_HOST", "127.0.0.1"),
        )


__all__ = ["CloudConfig", "MissingEnvError", "REQUIRED_VARS"]
