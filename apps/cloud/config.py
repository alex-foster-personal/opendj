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


@dataclass(frozen=True, repr=False)
class CloudConfig:
    """Runtime config for the cloud-sync daemon.

    Bind host defaults to ``127.0.0.1`` per D5 (CAT-05b). Any override is
    surfaced to the web UI via a warning banner.

    The default dataclass ``__repr__`` would include every field, which
    would leak ``R2_SECRET_ACCESS_KEY`` into any log or traceback that
    stringifies the instance. We opt out and provide a masked repr; see
    .planning/SECURITY-RED-TEAM-2026-04-17.md finding 3.
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

    def __repr__(self) -> str:
        # Mask secret + access key id so the value never shows up even in a
        # partial leak. Show only that a non-empty secret was loaded.
        masked = "***" if self.r2_secret_access_key else ""
        akid = self.r2_access_key_id
        akid_masked = f"{akid[:4]}***" if len(akid) > 4 else ("***" if akid else "")
        return (
            "CloudConfig("
            f"r2_account_id={self.r2_account_id!r}, "
            f"r2_access_key_id={akid_masked!r}, "
            f"r2_secret_access_key={masked!r}, "
            f"state_bucket={self.state_bucket!r}, "
            f"audio_bucket={self.audio_bucket!r}, "
            f"hostname={self.hostname!r}, "
            f"bind_host={self.bind_host!r})"
        )

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
