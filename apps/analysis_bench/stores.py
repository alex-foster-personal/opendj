"""Where a sealed bundle lives between hosts, and how it gets there safely.

Split out of `bundles.py`, which had grown to two jobs: what a bundle IS
(identity, sealing, verification) and where it TRAVELS. This is the second.

TWO TRANSPORTS, ONE CONTRACT. `DirStore` is a mounted or rsynced asset
directory, which is what bifrost2 offers and the only one this repo's tests can
exercise. `R2Store` is an S3-compatible bucket (Cloudflare R2) whose credentials
come from the environment and never from argv. Both replace a whole
lane/version prefix rather than overwriting file by file, because a bundle that
shrinks must not leave an object behind for a later pull to resurrect, and both
stage their replacement before touching the live prefix, because the prefix is
shared and a half-finished push is visible to every consumer on it.

NEITHER IS ATOMIC, and neither pretends to be. What they buy is the failure
mode: an interrupted push leaves a state `verify_bundle` refuses by checksum
rather than one it scores and reports.
"""

from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from apps.analysis_bench.bundles import (
    METADATA_NAMES,
    BundleError,
    safe_target,
    verify_bundle,
)

# Where `--store` comes from when the caller does not pass one. Named rather
# than defaulted to a path, so a host with no store configured refuses instead
# of inventing an empty one.
STORE_ENV = "MDT_BENCH_STORE"

# Every store keeps bundles under this prefix unless the URL names another.
DEFAULT_PREFIX = "bench"

# R2 credential names, in the order they are looked up. CF_R2_* is the
# `construct > dev_af` naming the spec points at; R2_* is what `apps.cloud`
# already reads, so a host holding either can push.
_ACCESS_KEYS = ("CF_R2_ACCESS_KEY_ID", "R2_ACCESS_KEY_ID")
_SECRET_KEYS = ("CF_R2_SECRET_ACCESS_KEY", "R2_SECRET_ACCESS_KEY")
_ACCOUNT_KEYS = ("CF_R2_ACCOUNT_ID", "R2_ACCOUNT_ID")
@dataclass(frozen=True)
class DirStore:
    """A filesystem-backed store: a mounted or rsynced asset directory."""

    root: Path
    prefix: str = DEFAULT_PREFIX

    def describe(self) -> str:
        return f"dir:{self.root}/{self.prefix}"

    def _at(self, lane: str, version: str) -> Path:
        return self.root / self.prefix / lane / version

    def upload(self, bundle: Path, lane: str, version: str) -> str:
        """Copy beside the target, then swap. Deleting first loses the live bundle.

        `rmtree` followed by `copytree` left the SHARED prefix empty or half
        populated for the whole copy, and on a mounted asset store holding
        excerpt audio that is minutes, not milliseconds. Every consumer on that
        mount saw the gap, and an interrupted copy left it there (Codex P2
        BLOCKING, PR #1582). The copy now lands in a sibling and the swap is two
        renames, with the old bundle put back if the second one fails.
        """
        target = self._at(lane, version)
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.with_name(f".staging-{uuid.uuid4().hex}")
        retired = target.with_name(f".retired-{uuid.uuid4().hex}")
        expendable = True
        try:
            shutil.copytree(bundle, staging)
            if target.exists():
                target.rename(retired)
                expendable = False
            staging.rename(target)
            expendable = True
        except OSError:
            expendable = self._restore(retired, target)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            if expendable:
                shutil.rmtree(retired, ignore_errors=True)
            else:
                print(
                    f"[bench] {target} is not whole and the previous bundle could not be put "
                    f"back automatically. It is KEPT at {retired}; move it back by hand.",
                    flush=True,
                )
        return str(target)

    @staticmethod
    def _restore(retired: Path, target: Path) -> bool:
        """Put the previous bundle back, reporting whether `retired` may be deleted.

        The rollback can fail too -- a permission change, a full mount, a
        vanished parent. Deleting the backup anyway would destroy the last valid
        shared bundle on the way out of an error path (Codex P2 BLOCKING,
        PR #1582), so False here means the caller must keep it and say where.
        """
        if target.exists() or not retired.is_dir():
            return True
        try:
            retired.rename(target)
        except OSError:
            return False
        return True

    def download(self, lane: str, version: str, dest: Path) -> None:
        source = self._at(lane, version)
        if not source.is_dir():
            raise BundleError(f"{source} is not in the store; nothing to pull")
        shutil.copytree(source, dest)


@dataclass(frozen=True)
class R2Store:
    """An S3-compatible bucket (Cloudflare R2). Credentials come from the env."""

    bucket: str
    prefix: str = DEFAULT_PREFIX

    def describe(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}"

    def _credential(self, names: tuple[str, ...]) -> str:
        for name in names:
            value = os.environ.get(name)
            if value:
                return value
        raise BundleError(
            f"{self.describe()} needs {names[0]} (or {names[1]}) in the environment. "
            "On a machine with Doppler access: "
            "`doppler run -p construct -c dev_af -- python -m apps.analysis_bench ...`"
        )

    def client(self) -> Any:
        access = self._credential(_ACCESS_KEYS)
        secret = self._credential(_SECRET_KEYS)
        account = self._credential(_ACCOUNT_KEYS)
        try:
            import boto3
        except ImportError as exc:
            raise BundleError(
                "boto3 is required to reach an S3 store: `uv sync --extra cloud`"
            ) from exc
        return boto3.client(
            "s3",
            endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
            aws_access_key_id=access,
            aws_secret_access_key=secret,
            region_name="auto",
        )

    def _key(self, lane: str, version: str, rel: str) -> str:
        return f"{self.prefix}/{lane}/{version}/{rel}"

    def _keys_under(self, client: Any, root: str) -> list[str]:
        pages = client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=root)
        return [obj["Key"] for page in pages for obj in page.get("Contents", [])]

    def _delete(self, client: Any, keys: list[str]) -> None:
        """Delete in the 1000-key batches the S3 API accepts."""
        for begin in range(0, len(keys), 1000):
            client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": key} for key in keys[begin : begin + 1000]]},
            )

    def upload(self, bundle: Path, lane: str, version: str) -> str:
        """Stage the whole bundle outside the live prefix, then publish it.

        Two failures gave this its shape (both Codex BLOCKING, PR #1582).
        Overwriting key by key left ORPHANS: a payload dropped or renamed by a
        second fixture set stayed in the bucket, `download` fetches every object
        under the prefix, verification then saw an unlisted extra, and every
        later pull failed until somebody cleaned the bucket by hand. Uploading
        straight into the live prefix also meant a push that DIED HALFWAY left
        new metadata over old payloads: a bundle nobody could pull, and nobody
        had asked for it.

        So every object goes to an isolated staging prefix first and the live
        prefix is untouched until the whole bundle is up there. Publication is
        server side, payloads before metadata, so the sealed `SHA256SUMS` and
        `BUNDLE_ID` land last and orphans are removed last of all. Staging is
        cleared either way.

        S3 offers no multi-object commit, so this is NOT atomic. A failure
        during the publish copies leaves the old metadata over a mixture of
        payloads: a bundle `verify_bundle` refuses by checksum, rather than one
        it scores and reports. That is the property worth having when
        atomicity is not on offer.
        """
        client = self.client()
        root = f"{self.prefix}/{lane}/{version}/"
        staging = f"{self.prefix}/.staging/{lane}/{version}/{uuid.uuid4().hex}/"
        rels = sorted(p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file())
        try:
            for rel in rels:
                client.upload_file(str(bundle / rel), self.bucket, f"{staging}{rel}")
            before = set(self._keys_under(client, root))
            written = set()
            for rel in sorted(rels, key=lambda name: name in METADATA_NAMES):
                client.copy_object(
                    Bucket=self.bucket,
                    Key=f"{root}{rel}",
                    CopySource={"Bucket": self.bucket, "Key": f"{staging}{rel}"},
                )
                written.add(f"{root}{rel}")
            self._delete(client, sorted(before - written))
        finally:
            self._delete(client, self._keys_under(client, staging))
        return f"s3://{self.bucket}/{self.prefix}/{lane}/{version}"

    def download(self, lane: str, version: str, dest: Path) -> None:
        client = self.client()
        root = f"{self.prefix}/{lane}/{version}/"
        keys = self._keys_under(client, root)
        if not keys:
            raise BundleError(f"s3://{self.bucket}/{root} holds no objects; nothing to pull")
        for key in keys:
            target = safe_target(dest, key[len(root) :])
            target.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(self.bucket, key, str(target))


Store = DirStore | R2Store


def resolve_store(url: str | None) -> Store:
    """Turn a store URL (or `MDT_BENCH_STORE`) into a transport, or refuse by name."""
    url = url or os.environ.get(STORE_ENV)
    if not url:
        raise BundleError(
            f"no bundle store: pass --store or set {STORE_ENV}. Accepted forms are "
            "`s3://<bucket>[/<prefix>]` for Cloudflare R2 and a directory path "
            "(or `file:///path`) for a mounted asset store such as bifrost2's."
        )
    parsed = urlparse(url)
    if parsed.scheme == "s3":
        return R2Store(bucket=parsed.netloc, prefix=parsed.path.strip("/") or DEFAULT_PREFIX)
    if parsed.scheme in ("", "file"):
        root = Path(parsed.path if parsed.scheme == "file" else url).expanduser()
        return DirStore(root=root)
    raise BundleError(f"unsupported bundle store {url!r}: use `s3://...`, `file://...` or a path")


# ----- Push and pull ------------------------------------------------------


def push_bundle(bundle: Path, store: Store, *, lane: str, version: str) -> str:
    """Verify locally, then upload. Pushing an unverified bundle poisons every puller."""
    manifest = verify_bundle(Path(bundle))
    if manifest["lane"] != lane or manifest["version"] != version:
        raise BundleError(
            f"{bundle} is {manifest['lane']}/{manifest['version']}, "
            f"not the {lane}/{version} it was asked to push as"
        )
    return store.upload(Path(bundle), lane, version)


def pull_bundle(
    store: Store, *, lane: str, version: str, dest: Path, expect_bundle_id: str | None = None
) -> Path:
    """Download and verify, leaving nothing behind when the checksums disagree."""
    dest = Path(dest)
    if dest.exists():
        raise BundleError(f"{dest} already exists; remove it or pull somewhere else")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        store.download(lane, version, dest)
        verify_bundle(dest, expect_bundle_id=expect_bundle_id)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest
