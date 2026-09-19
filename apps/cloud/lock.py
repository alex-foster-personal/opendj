"""Cooperative single-writer lock on R2 (D2).

Lock object is a tiny JSON blob at ``s3://<state_bucket>/LOCK.json``.
All S3 calls go through a narrow :class:`S3Client` protocol so tests can
inject a :class:`FakeS3Client` with no network I/O. In production the caller
supplies a ``boto3`` or ``aioboto3`` client wrapper that implements the same
five methods.

Semantics (per plan 11-01 step 3):

* ``try_acquire()`` -- conditional PUT (``IfNoneMatch='*'``). If another
  holder's object is still present, read it; if expired (expires_at <= now),
  CAS-overwrite by etag. Otherwise return ``AcquireResult(success=False)``.
* ``heartbeat()`` -- CAS-refresh ``expires_at`` every 2 minutes.
* ``release()`` -- conditional DELETE by etag. Never deletes if we do not
  currently hold the lock.
* SIGTERM / SIGINT / atexit -- release via :meth:`install_signal_handlers`.

Lock JSON shape::

    {
      "holder": "mbp",
      "acquired_at": "2026-04-17T14:02:03Z",
      "expires_at":  "2026-04-17T14:12:03Z",
      "pid": 12345,
      "version": 1
    }
"""
from __future__ import annotations

import atexit
import json
import os
import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .config import CloudConfig

LOCK_KEY: str = "LOCK.json"
LOCK_TTL_SECONDS: int = 600  # 10 minutes (per D2)
HEARTBEAT_SECONDS: int = 120  # 2 minutes
LOCK_VERSION: int = 1


class LockError(RuntimeError):
    """Base class for lock-related errors."""


class LockLostError(LockError):
    """Raised when a heartbeat or release finds we no longer hold the lock.

    This happens if (a) another host stole the lock after our TTL expired,
    or (b) someone manually removed the object. The caller MUST terminate
    the Litestream subprocess immediately.
    """


@dataclass(frozen=True)
class LockHolder:
    """Deserialised contents of ``LOCK.json``."""

    holder: str
    acquired_at: datetime
    expires_at: datetime
    pid: int
    version: int

    def is_expired(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        return now >= self.expires_at

    def to_json(self) -> str:
        return json.dumps(
            {
                "holder": self.holder,
                "acquired_at": _isoformat(self.acquired_at),
                "expires_at": _isoformat(self.expires_at),
                "pid": self.pid,
                "version": self.version,
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> "LockHolder":
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        return cls(
            holder=str(data["holder"]),
            acquired_at=_parse_iso(data["acquired_at"]),
            expires_at=_parse_iso(data["expires_at"]),
            pid=int(data["pid"]),
            version=int(data.get("version", 1)),
        )


@dataclass(frozen=True)
class AcquireResult:
    success: bool
    holder: LockHolder | None = None
    etag: str | None = None
    error: str | None = None


def _isoformat(dt: datetime) -> str:
    """UTC isoformat ending in 'Z' (Litestream-friendly)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(raw: str) -> datetime:
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    return datetime.fromisoformat(raw).astimezone(UTC)


# --- S3 client protocol -------------------------------------------------


class S3Client(Protocol):
    """Minimal S3-compatible interface used by :class:`Lock`.

    Implementations:
      * production: a thin wrapper around boto3 or aioboto3 sync calls.
      * tests: :class:`FakeS3Client` -- in-memory dict with etags.
    """

    def get_object(self, bucket: str, key: str) -> tuple[bytes, str] | None:
        """Return ``(body, etag)`` or ``None`` if the object does not exist."""

    def put_object_if_none_match(
        self, bucket: str, key: str, body: bytes
    ) -> tuple[bool, str | None]:
        """Create if absent. Returns ``(created, new_etag)``.

        ``created=False`` means the key already existed; in that case the
        caller should re-read to learn the holder.
        """

    def put_object_if_match(
        self, bucket: str, key: str, body: bytes, etag: str
    ) -> tuple[bool, str | None]:
        """CAS write. Returns ``(ok, new_etag)``. ``ok=False`` on mismatch.

        ``etag="*"`` is the S3 wildcard meaning "match any existing
        object" (i.e. succeed iff the key exists, regardless of its
        current etag). Any other string is a strict etag match.
        """

    def delete_object_if_match(
        self, bucket: str, key: str, etag: str
    ) -> bool:
        """CAS delete. Returns ``True`` on success, ``False`` on mismatch."""


class FakeS3Client:
    """Thread-safe in-memory S3 for tests.

    Maintains ``etag`` as ``sha1(body)`` so CAS is predictable. No pagination,
    no bucket creation -- buckets are implicit.
    """

    def __init__(self) -> None:
        import hashlib

        self._store: dict[tuple[str, str], tuple[bytes, str]] = {}
        self._lock = threading.Lock()
        self._hashlib = hashlib

    def _etag(self, body: bytes) -> str:
        return '"' + self._hashlib.sha1(body).hexdigest() + '"'

    def get_object(self, bucket: str, key: str) -> tuple[bytes, str] | None:
        with self._lock:
            got = self._store.get((bucket, key))
            return (got[0], got[1]) if got else None

    def put_object_if_none_match(
        self, bucket: str, key: str, body: bytes
    ) -> tuple[bool, str | None]:
        with self._lock:
            if (bucket, key) in self._store:
                return False, None
            etag = self._etag(body)
            self._store[(bucket, key)] = (body, etag)
            return True, etag

    def put_object_if_match(
        self, bucket: str, key: str, body: bytes, etag: str
    ) -> tuple[bool, str | None]:
        with self._lock:
            existing = self._store.get((bucket, key))
            if existing is None:
                return False, None
            # ``etag="*"`` is the S3 wildcard: match any existing object.
            # Any other value is a strict etag comparison.
            if etag != "*" and existing[1] != etag:
                return False, None
            new_etag = self._etag(body)
            self._store[(bucket, key)] = (body, new_etag)
            return True, new_etag

    def delete_object_if_match(
        self, bucket: str, key: str, etag: str
    ) -> bool:
        with self._lock:
            existing = self._store.get((bucket, key))
            if existing is None or existing[1] != etag:
                return False
            del self._store[(bucket, key)]
            return True


# --- Lock ----------------------------------------------------------------


class Lock:
    """High-level lock API. Pass any :class:`S3Client`-compatible client.

    One instance per process. Call :meth:`install_signal_handlers` once to
    hook SIGTERM / SIGINT / atexit release.
    """

    def __init__(
        self,
        cfg: CloudConfig,
        s3: S3Client,
        *,
        ttl_seconds: int = LOCK_TTL_SECONDS,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.cfg = cfg
        self.s3 = s3
        self.ttl_seconds = ttl_seconds
        self._now = now_fn or (lambda: datetime.now(UTC))
        self._held: tuple[LockHolder, str] | None = None  # (holder, etag)
        self._mutex = threading.Lock()

    # ----- internal helpers ---------------------------------------------

    def _build_holder(self) -> LockHolder:
        now = self._now()
        return LockHolder(
            holder=self.cfg.hostname,
            acquired_at=now,
            expires_at=now + timedelta(seconds=self.ttl_seconds),
            pid=os.getpid(),
            version=LOCK_VERSION,
        )

    def _refresh_holder(self, holder: LockHolder) -> LockHolder:
        now = self._now()
        return LockHolder(
            holder=holder.holder,
            acquired_at=holder.acquired_at,
            expires_at=now + timedelta(seconds=self.ttl_seconds),
            pid=holder.pid,
            version=holder.version,
        )

    # ----- public API ---------------------------------------------------

    def current_holder(self) -> LockHolder | None:
        """Read the current lock object (or None if absent / unparseable)."""
        got = self.s3.get_object(self.cfg.state_bucket, LOCK_KEY)
        if got is None:
            return None
        body, _ = got
        try:
            return LockHolder.from_json(body)
        except Exception:
            return None

    def try_acquire(self) -> AcquireResult:
        """Attempt to claim the lock; return success or the current holder.

        Logic:
          1. Conditional PUT with ``IfNoneMatch='*'``.
          2. On 412/409-style failure, read the existing object.
             If it is expired (TTL elapsed), CAS-overwrite by etag.
          3. Otherwise return ``success=False`` with holder info.
        """
        with self._mutex:
            candidate = self._build_holder()
            body = candidate.to_json().encode("utf-8")
            created, etag = self.s3.put_object_if_none_match(
                self.cfg.state_bucket, LOCK_KEY, body
            )
            if created and etag is not None:
                self._held = (candidate, etag)
                return AcquireResult(success=True, holder=candidate, etag=etag)

            # Object already exists -- inspect it.
            got = self.s3.get_object(self.cfg.state_bucket, LOCK_KEY)
            if got is None:
                # Raced: gone between PUT rejection and GET. One retry.
                created, etag = self.s3.put_object_if_none_match(
                    self.cfg.state_bucket, LOCK_KEY, body
                )
                if created and etag is not None:
                    self._held = (candidate, etag)
                    return AcquireResult(
                        success=True, holder=candidate, etag=etag
                    )
                return AcquireResult(
                    success=False, error="race on acquire; retry later"
                )
            existing_body, existing_etag = got
            try:
                existing_holder = LockHolder.from_json(existing_body)
            except Exception:
                # Corrupt lock. Overwrite by etag.
                ok, new_etag = self.s3.put_object_if_match(
                    self.cfg.state_bucket, LOCK_KEY, body, existing_etag
                )
                if ok and new_etag is not None:
                    self._held = (candidate, new_etag)
                    return AcquireResult(
                        success=True, holder=candidate, etag=new_etag
                    )
                return AcquireResult(
                    success=False,
                    error="corrupt lock object and CAS overwrite failed",
                )

            if existing_holder.is_expired(self._now()):
                ok, new_etag = self.s3.put_object_if_match(
                    self.cfg.state_bucket, LOCK_KEY, body, existing_etag
                )
                if ok and new_etag is not None:
                    self._held = (candidate, new_etag)
                    return AcquireResult(
                        success=True, holder=candidate, etag=new_etag
                    )
                return AcquireResult(
                    success=False,
                    holder=existing_holder,
                    error="expired lock takeover lost CAS race",
                )

            return AcquireResult(success=False, holder=existing_holder)

    def heartbeat(self) -> None:
        """Refresh ``expires_at`` on the lock.

        Raises :class:`LockLostError` if our etag no longer matches.
        """
        with self._mutex:
            if self._held is None:
                raise LockLostError("no local lock state; cannot heartbeat")
            holder, etag = self._held
            refreshed = self._refresh_holder(holder)
            body = refreshed.to_json().encode("utf-8")
            ok, new_etag = self.s3.put_object_if_match(
                self.cfg.state_bucket, LOCK_KEY, body, etag
            )
            if not ok or new_etag is None:
                self._held = None
                raise LockLostError(
                    "heartbeat CAS failed; another host has the lock"
                )
            self._held = (refreshed, new_etag)

    def release(self) -> bool:
        """Release the lock if we still hold it. Idempotent.

        Returns ``True`` if the object was removed, ``False`` otherwise
        (e.g. we never acquired or the etag moved).
        """
        with self._mutex:
            if self._held is None:
                return False
            _, etag = self._held
            self._held = None
            return self.s3.delete_object_if_match(
                self.cfg.state_bucket, LOCK_KEY, etag
            )

    def install_signal_handlers(self) -> None:
        """Wire SIGTERM / SIGINT / atexit to :meth:`release`.

        Safe to call multiple times; replaces any previous handler we set.
        Does not replace non-default handlers from third parties.
        """
        atexit.register(self._safe_release)
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                prior = signal.getsignal(sig)
                signal.signal(sig, self._make_signal_handler(prior))
            except (ValueError, OSError):
                # e.g. not on main thread; skip silently.
                pass

    def _safe_release(self) -> None:
        try:
            self.release()
        except Exception:
            pass

    def _make_signal_handler(self, prior: Any) -> Callable[[int, Any], None]:
        def handler(signum: int, frame: Any) -> None:
            self._safe_release()
            if callable(prior):
                prior(signum, frame)
            elif prior in (signal.SIG_DFL, signal.SIG_IGN, None):
                raise SystemExit(128 + signum)
        return handler


__all__ = [
    "AcquireResult",
    "FakeS3Client",
    "LockError",
    "LockHolder",
    "LockLostError",
    "Lock",
    "S3Client",
    "LOCK_KEY",
    "LOCK_TTL_SECONDS",
    "HEARTBEAT_SECONDS",
]
