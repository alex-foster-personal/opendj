"""Litestream subprocess supervisor with R2 lock integration.

Runs as ``python -m apps.cloud.replicate`` under ``doppler run``. The process:

1. Loads :class:`CloudConfig` from environment.
2. Acquires the cooperative lock (``apps.cloud.lock.Lock``).
3. If the lock is held elsewhere, prints holder info and exits 2.
4. Spawns ``litestream replicate -config apps/cloud/litestream.yml``.
5. Heartbeats the lock every ``HEARTBEAT_SECONDS``.
6. On SIGTERM/SIGINT/normal exit, terminates the subprocess and releases the
   lock.

A ``--self-check`` flag runs the startup phase (config load + lock attempt
against a fake S3) and exits cleanly. Useful for CI smoke tests.

S3Client injection: this module does NOT import boto3 directly. Callers in
production must import ``apps.cloud.lock`` + an S3 adapter; we ship a thin
:func:`boto3_s3_client` factory that lazy-imports boto3.
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Protocol

from .config import CloudConfig, MissingEnvError
from .lock import (
    HEARTBEAT_SECONDS,
    AcquireResult,
    FakeS3Client,
    Lock,
    LockLostError,
    S3Client,
)

log = logging.getLogger(__name__)

DEFAULT_LITESTREAM_BIN: str = "litestream"
DEFAULT_CONFIG_PATH: str = "apps/cloud/litestream.yml"


class SubprocessLike(Protocol):
    """Minimal subprocess.Popen-ish surface so tests can inject a fake."""

    returncode: int | None
    pid: int

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


SubprocessFactory = Callable[[list[str]], SubprocessLike]


def _default_subprocess_factory(argv: list[str]) -> SubprocessLike:
    """Spawn ``litestream replicate`` inheriting stdout/stderr."""
    return subprocess.Popen(argv)  # type: ignore[return-value]


def _boto3_get_object(client, ClientError, bucket: str, key: str):  # pragma: no cover (IO)
    try:
        resp = client.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404"):
            return None
        raise
    return resp["Body"].read(), resp["ETag"]


def _boto3_put_if_none(client, ClientError, bucket: str, key: str, body: bytes):
    try:
        resp = client.put_object(Bucket=bucket, Key=key, Body=body, IfNoneMatch="*")
        return True, resp["ETag"]
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("PreconditionFailed", "412"):
            return False, None
        raise


def _boto3_put_if_match(client, ClientError, bucket: str, key: str, body: bytes, etag: str):
    try:
        resp = client.put_object(Bucket=bucket, Key=key, Body=body, IfMatch=etag)
        return True, resp["ETag"]
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("PreconditionFailed", "412"):
            return False, None
        raise


def _boto3_delete_if_match(client, ClientError, bucket: str, key: str, etag: str) -> bool:
    try:
        client.delete_object(Bucket=bucket, Key=key, IfMatch=etag)
        return True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("PreconditionFailed", "412", "NoSuchKey"):
            return False
        raise


def _boto3_adapter(client, ClientError):  # pragma: no cover (IO)
    class _Boto3Adapter:
        def get_object(self, bucket: str, key: str):
            return _boto3_get_object(client, ClientError, bucket, key)

        def put_object_if_none_match(self, bucket: str, key: str, body: bytes):
            return _boto3_put_if_none(client, ClientError, bucket, key, body)

        def put_object_if_match(self, bucket: str, key: str, body: bytes, etag: str):
            return _boto3_put_if_match(client, ClientError, bucket, key, body, etag)

        def delete_object_if_match(self, bucket: str, key: str, etag: str):
            return _boto3_delete_if_match(client, ClientError, bucket, key, etag)

    return _Boto3Adapter()


def boto3_s3_client(cfg: CloudConfig) -> S3Client:  # pragma: no cover (IO)
    """Build a sync S3Client adapter over boto3.

    Not exercised by the unit test suite (requires network); exists so
    ``apps.cloud.replicate`` can be invoked in production without a fake.
    """
    try:
        import boto3  # type: ignore
        from botocore.exceptions import ClientError  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "boto3 is required for production cloud sync; "
            "install via `pip install boto3 botocore`."
        ) from exc

    client = boto3.client(
        "s3",
        endpoint_url=cfg.r2_endpoint,
        aws_access_key_id=cfg.r2_access_key_id,
        aws_secret_access_key=cfg.r2_secret_access_key,
        region_name="auto",
    )

    return _boto3_adapter(client, ClientError)


class Replicator:
    """Supervises a Litestream subprocess under a cooperative lock."""

    def __init__(
        self,
        cfg: CloudConfig,
        s3: S3Client,
        *,
        config_path: str = DEFAULT_CONFIG_PATH,
        litestream_bin: str = DEFAULT_LITESTREAM_BIN,
        subprocess_factory: SubprocessFactory = _default_subprocess_factory,
        heartbeat_seconds: int = HEARTBEAT_SECONDS,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cfg = cfg
        self.lock = Lock(cfg, s3)
        self.config_path = config_path
        self.litestream_bin = litestream_bin
        self._factory = subprocess_factory
        self._heartbeat_seconds = heartbeat_seconds
        self._sleep = sleep_fn
        self._proc: SubprocessLike | None = None
        self._stop = threading.Event()

    def _argv(self) -> list[str]:
        return [
            self.litestream_bin,
            "replicate",
            "-config",
            self.config_path,
        ]

    def start(self) -> int:
        """Acquire lock + spawn litestream. Returns process exit code.

        Exits with:
          0  -- normal shutdown after SIGTERM / SIGINT
          2  -- another host holds the lock
          3  -- lock was stolen mid-run
          >0 -- forwarded subprocess exit code
        """
        acq: AcquireResult = self.lock.try_acquire()
        if not acq.success:
            holder = acq.holder
            if holder is not None:
                sys.stderr.write(
                    f"Cloud lock held by {holder.holder} (pid {holder.pid}), "
                    f"expires {holder.expires_at.isoformat()}. "
                    f"Refusing to start Litestream.\n"
                )
            else:
                sys.stderr.write(
                    f"Could not acquire cloud lock: {acq.error}\n"
                )
            return 2

        self.lock.install_signal_handlers()
        try:
            self._proc = self._factory(self._argv())
            return self._supervise_loop()
        finally:
            # Order matters (codex P11-F02): ensure Litestream has fully
            # exited (flushing any pending WAL frames to R2) BEFORE we drop
            # the cloud lock. Releasing early opens a window where another
            # writer can acquire the lock while our litestream is still
            # flushing, corrupting the replicated state.
            self._terminate_proc()
            self._wait_for_proc_exit(timeout=30.0)
            self.lock.release()

    def _supervise_loop(self) -> int:
        """Heartbeat + poll subprocess until exit or lock loss."""
        assert self._proc is not None
        proc = self._proc
        next_heartbeat = time.monotonic() + self._heartbeat_seconds
        while not self._stop.is_set():
            rc = proc.poll()
            if rc is not None:
                return rc
            now = time.monotonic()
            if now >= next_heartbeat:
                try:
                    self.lock.heartbeat()
                except LockLostError as exc:
                    sys.stderr.write(
                        f"Cloud lock stolen mid-run: {exc}. "
                        "Terminating Litestream.\n"
                    )
                    return 3
                next_heartbeat = now + self._heartbeat_seconds
            self._sleep(0.5)
        return 0

    def _terminate_proc(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except Exception:
                    pass
        except Exception:
            pass

    def _wait_for_proc_exit(self, timeout: float) -> None:
        """Block until the litestream subprocess has fully exited.

        We must not release the cloud lock while litestream may still be
        flushing WAL frames to R2 (codex P11-F02). ``_terminate_proc`` only
        signals SIGTERM and waits a few seconds; this method gives the
        process the full configured budget to flush before we move on.
        """
        if self._proc is None:
            return
        deadline = time.monotonic() + timeout
        try:
            while self._proc.poll() is None and time.monotonic() < deadline:
                self._sleep(0.05)
            if self._proc.poll() is None:
                # Still running after timeout: hard-kill to guarantee no
                # writer races us for the lock.
                try:
                    self._proc.kill()
                except Exception:
                    pass
                try:
                    self._proc.wait(timeout=5)
                except Exception:
                    pass
        except Exception:
            pass

    def request_stop(self) -> None:
        """Ask the supervise loop to exit cleanly (used in tests)."""
        self._stop.set()


def self_check(_argv: list[str] | None = None) -> int:
    """Run a dry-run smoke check against :class:`FakeS3Client`.

    Useful for CI: no network, no litestream binary required.
    """
    import os as _os

    env = dict(_os.environ)
    env.setdefault("R2_ACCOUNT_ID", "selfcheck-acct")
    env.setdefault("R2_ACCESS_KEY_ID", "selfcheck-id")
    env.setdefault("R2_SECRET_ACCESS_KEY", "selfcheck-secret")
    cfg = CloudConfig.from_env(env)
    fake = FakeS3Client()
    lock = Lock(cfg, fake)
    result = lock.try_acquire()
    if not result.success:
        sys.stderr.write(f"self-check: acquire failed: {result}\n")
        return 1
    holder = lock.current_holder()
    if holder is None or holder.holder != cfg.hostname:
        sys.stderr.write("self-check: holder mismatch\n")
        return 1
    lock.release()
    sys.stdout.write(
        f"self-check OK: config loads, lock acquires, holder={holder.holder}\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="apps.cloud.replicate",
        description="Litestream supervisor with R2 lock.",
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="Run a network-free smoke test and exit.",
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help="Path to litestream.yml (default: apps/cloud/litestream.yml).",
    )
    parser.add_argument(
        "--litestream-bin",
        default=DEFAULT_LITESTREAM_BIN,
        help="Path to litestream binary (default: 'litestream' on PATH).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    if args.self_check:
        return self_check()

    try:
        cfg = CloudConfig.from_env()
    except MissingEnvError as exc:  # pragma: no cover - env path
        sys.stderr.write(f"{exc}\n")
        return 1

    s3 = boto3_s3_client(cfg)  # pragma: no cover - network path
    rep = Replicator(
        cfg, s3, config_path=args.config, litestream_bin=args.litestream_bin
    )
    return rep.start()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_LITESTREAM_BIN",
    "Replicator",
    "SubprocessFactory",
    "SubprocessLike",
    "boto3_s3_client",
    "main",
    "self_check",
]
