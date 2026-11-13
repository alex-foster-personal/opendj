from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path

try:
    from scripts.fixtures.pack_common import (
        CACHE,
        IMPORTER_ASSET,
        META_ASSET,
        PREFIX,
        PROJECT_FILES,
        REPO,
        SHARED,
        ZIP_ASSET,
        _gh,
        _log,
        _releases,
        _sha256,
        _tag_ok,
    )
except ImportError:
    from pack_common import (
        CACHE,
        IMPORTER_ASSET,
        META_ASSET,
        PREFIX,
        PROJECT_FILES,
        REPO,
        SHARED,
        ZIP_ASSET,
        _gh,
        _log,
        _releases,
        _sha256,
        _tag_ok,
    )


def _part(dest: Path) -> Path:
    """A temp name no other process or container writes (the shared folder spans sessions)."""
    return dest.with_name(f"{dest.name}.{socket.gethostname()}.{os.getpid()}.part")


def _copy_atomic(src: Path, dest: Path) -> None:
    part = _part(dest)
    shutil.copyfile(src, part)
    part.replace(dest)


def _write_atomic(dest: Path, text: str) -> None:
    """A reader in another session sees the whole file or none of it."""
    part = _part(dest)
    part.write_text(text, encoding="utf-8", newline="\n")
    part.replace(dest)


def _fetch_asset(release: dict, name: str, dest: Path) -> None:
    asset = next((x for x in release["assets"] if x["name"] == name), None)
    if not asset:
        raise RuntimeError(f"{release['tag_name']} has no {name} asset")
    part = _part(dest)
    with open(part, "wb") as f:
        _gh("-H", "Accept: application/octet-stream", f"repos/{REPO}/releases/assets/{asset['id']}", stdout=f)
    part.replace(dest)


def _read_meta(d: Path) -> dict | None:
    """pack.json, or None if it is missing, truncated or lacks the hashes and ids ensure checks."""
    try:
        meta = json.loads((d / META_ASSET).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or not all(isinstance(meta.get(k), str) for k in ("sha256", "importer_sha256")):
        return None
    ids = meta.get("stable_ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        return None
    # Every pack lives in a directory named by its tag. A pack.json naming another
    # tag is an old pack copied under a newer name, which newest-wins would then
    # advertise as the newer fixture.
    if meta.get("tag") != d.name:
        return None
    return meta


def _complete(d: Path) -> bool:
    return (d / IMPORTER_ASSET).exists() and (meta := _read_meta(d)) is not None and _whole_zip(d, meta)


def _whole_zip(d: Path, meta: dict) -> bool:
    """The zip is all there: as many bytes as pack.json records.

    The project folder is a network mount that can show a large file while its
    upload from another session is still in flight; a short one is neither used
    nor judged damaged, since reading it again later may well find it whole.
    """
    try:
        return (d / ZIP_ASSET).stat().st_size == meta.get("bytes")
    except OSError:
        return False


def _cached_tags() -> set[str]:
    return {
        p.name
        for base in (CACHE, SHARED)
        if base.is_dir()
        for p in base.glob(PREFIX + "*")
        if _tag_ok(p.name) and _complete(p)
    }


def _resolve(tag: str | None) -> tuple[str, Path]:
    """Return (tag, dir holding odj-fixture.zip + fixture.py + pack.json), fetching if needed.

    Candidates are GitHub releases plus packs already in the local or shared cache;
    without --tag the newest tag wins (tags sort by their UTC timestamp).
    """
    try:
        remote = {r["tag_name"]: r for r in _releases()}
    except RuntimeError as e:
        remote = {}
        _log(f"GitHub unavailable, using caches only: {str(e).splitlines()[0][:200]}")
    known = set(remote) | _cached_tags()
    if tag and tag not in known:
        raise RuntimeError(f"no pack {tag} on GitHub ({REPO}) or in {CACHE} / {SHARED}")
    if not tag:
        if not known:
            raise RuntimeError(
                f"no {PREFIX}* pack on GitHub ({REPO}) or in {CACHE} / {SHARED}; publish one from Silver"
            )
        tag = max(known)
    local = CACHE / tag
    for src in (local, SHARED / tag):
        if _complete(src):
            if src != local:
                local.mkdir(parents=True, exist_ok=True)
                for n in (META_ASSET, IMPORTER_ASSET, ZIP_ASSET):
                    # A truncated local pack.json is replaced, never kept.
                    if not (local / n).exists() or (n == META_ASSET and _read_meta(local) is None):
                        _copy_atomic(src / n, local / n)
            return tag, local
    rel = remote[tag]
    local.mkdir(parents=True, exist_ok=True)
    for n in (META_ASSET, IMPORTER_ASSET, ZIP_ASSET):
        _log(f"downloading {tag}/{n}")
        _fetch_asset(rel, n, local / n)
    fetched = _read_meta(local)
    if fetched is None:
        (local / META_ASSET).unlink(missing_ok=True)
        raise RuntimeError(f"downloaded {tag}/{META_ASSET} is unreadable; removed, rerun to refetch")
    want = fetched["sha256"]
    got = _sha256(local / ZIP_ASSET)
    if got != want:
        (local / ZIP_ASSET).unlink()
        raise RuntimeError(
            f"downloaded {tag} zip fails its sha256 ({got[:12]} != {want[:12]}); removed, rerun to refetch"
        )
    # The project mount is the signal, not fixtures/ under it: the first fetch in
    # a fresh project creates fixtures/packs so later sessions skip the download.
    if SHARED.parent.is_dir() or (SHARED.is_relative_to(PROJECT_FILES) and PROJECT_FILES.is_dir()):
        try:
            (SHARED / tag).mkdir(parents=True, exist_ok=True)
            for n in (IMPORTER_ASSET, ZIP_ASSET, META_ASSET):
                _copy_atomic(local / n, SHARED / tag / n)
            _log(f"cached in {SHARED / tag} for other sessions in this project")
        except OSError as e:
            _log(f"could not fill the shared cache: {e}")
    return tag, local


LOCK_STALE_S = 300
LOCK_BEAT_S = 30


class _Lock:
    """mkdir lock (atomic on every OS) with an owner token and a heartbeat.

    The holder touches the lock every LOCK_BEAT_S, so only a holder that died
    goes stale; release removes the lock only while the token is still ours.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.token = uuid.uuid4().hex
        self._stop = threading.Event()
        self._beat = threading.Thread(target=self._heartbeat, daemon=True)

    def _heartbeat(self) -> None:
        while not self._stop.wait(LOCK_BEAT_S):
            if self._owner() != self.token:
                return  # never keep someone else's lock fresh
            try:
                os.utime(self.path)
            except OSError:
                return

    def _stale(self, path: Path | None = None) -> bool:
        try:
            return time.time() - (path or self.path).stat().st_mtime > LOCK_STALE_S
        except FileNotFoundError:
            return False

    def _owner(self) -> str | None:
        try:
            return (self.path / "owner").read_text(encoding="utf-8")
        except OSError:
            return None

    def acquire(self, wait_s: float = 3600) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + wait_s
        while True:
            try:
                self.path.mkdir()
            except FileExistsError:
                if self._stale():
                    self._take_over()
                    continue
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        f"another ensure has held {self.path} too long; remove it if no ensure is running"
                    ) from None
                time.sleep(2)
            else:
                (self.path / "owner").write_text(self.token, encoding="utf-8", newline="\n")
                self._beat.start()
                return

    def _take_over(self) -> None:
        """Remove a stale lock, one contender at a time.

        Two processes can both see the same stale lock. Without the guard the
        slower one would delete the fresh lock the faster one just made, and
        both would import into the same paths. The staleness check is repeated
        while holding the guard, so the second contender sees the new lock and
        waits. A guard left by a contender that died is itself stale after
        LOCK_STALE_S.
        """
        guard = self.path.with_name(self.path.name + ".takeover")
        try:
            guard.mkdir()
        except FileExistsError:
            if self._stale(guard):
                shutil.rmtree(guard, ignore_errors=True)
            else:
                time.sleep(1)
            return
        try:
            if self._stale():
                _log(f"taking over a stale lock {self.path}")
                shutil.rmtree(self.path, ignore_errors=True)
        finally:
            shutil.rmtree(guard, ignore_errors=True)

    def release(self) -> None:
        self._stop.set()
        if self._owner() == self.token:
            shutil.rmtree(self.path, ignore_errors=True)
