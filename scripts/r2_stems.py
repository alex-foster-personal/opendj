"""Shared R2 access for the stem rails.

Three jobs move stem bundles between stores and all three need the same client
and the same listing: ``stem_inventory.py`` (report what is published),
``local_stems_to_r2.py`` (publish from the Mac) and, historically, the
bifrost2 pair. Each used to carry its own copy, which is how
``r2_stem_sync.py`` and ``b2_stems_to_r2.py`` came to share a credential-order
bug that had to be fixed twice in one PR.

Deliberately free of any dependency on the bundle model, so the discovery
layer can import the key prefix from here without a cycle.

-Claude
"""
from __future__ import annotations

import os
from typing import Any

R2_ENV_KEYS: tuple[str, str, str] = (
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
)
DEFAULT_BUCKET: str = "music-dj-audio"

#: Pre-unification path-addressed stems layout. Cloudsync issue #1452 chose
#: content addressing as THE R2 layout (R2_CONTENT_ADDRESSED_LAYOUT below);
#: ``stems/<preset>/<stable_id>/<file>`` survives here ONLY as the staging
#: namespace for the one-shot archival rails (scripts/local_stems_to_r2.py,
#: scripts/b2_stems_to_r2.py), which publish PRE-EXISTING legacy bundles and
#: whose output scripts/rekey_stems_layout.py re-keys to content addressing.
#: New R2 objects from the farm are content-addressed and never written under
#: this prefix; it is not a write target for any continuous producer.
R2_STEM_PREFIX: str = "stems"  # legacy staging namespace (archival rails only)

#: The one content-addressed layout (mirrored from apps/cloud/r2_keys.py;
#: a parity test pins the two to the same string so this copy cannot drift).
#: A producer that has the bytes derives the key from the body's SHA-256, so
#: identical artifacts deduplicate and every fetch is integrity-checkable.
R2_CONTENT_ADDRESSED_LAYOUT: str = "assets/{sha256[:2]}/{sha256}"

_HEX_DIGITS: str = "0123456789abcdef"


def content_addressed_key(content_sha256: str) -> str:
    """Return ``assets/<sha256[:2]>/<sha256>`` for a bare 64-hex digest.

    Mirrors ``apps.cloud.asset_store.asset_object_key`` for the stem rails.
    The farm container and these scripts cannot always import the app layer,
    so this copy is pinned to it by a parity test instead of by import.
    """
    digest = content_sha256.strip()
    if (
        len(digest) != 64
        or any(char not in _HEX_DIGITS for char in digest)
    ):
        raise ValueError(
            f"content_sha256 must be 64 lowercase hex chars; got "
            f"{content_sha256!r}"
        )
    return f"assets/{digest[:2]}/{digest}"


def r2_client(invocation: str = "scripts/stem_inventory.py --check-r2") -> Any:
    """S3 client for R2, refusing early and by name when credentials are absent.

    ``invocation`` is echoed in the error so the message tells the reader how
    to re-run the script they actually ran. A shared helper that always named
    its own module would send the caller of a sibling job to the wrong command.

    The credential check runs BEFORE the boto3 import for the reason given in
    scripts/b2_stems_to_r2.py: boto3 is a PEP 723 inline dependency, so
    importing first turns a forgotten ``doppler run`` into a
    ModuleNotFoundError that says nothing about the real problem.
    """
    missing = [key for key in R2_ENV_KEYS if not os.environ.get(key)]
    if missing:
        raise SystemExit(
            f"error: {missing} not in the environment. Run under:\n"
            "  doppler run --project general --config dev_personal -- "
            f"uv run {invocation}\n"
            "If those secrets do not exist, mint an R2 API token (Object Read "
            "& Write, scoped to the music-dj-audio bucket) at\n"
            "  Cloudflare dashboard > R2 > API > Manage API tokens\n"
            "and store it in Doppler general/dev_personal under those names."
        )

    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(retries={"max_attempts": 3, "mode": "standard"}),
    )


def list_r2_sizes(client: Any, bucket: str) -> dict[str, int]:
    """{key: size} for every stem object in the bucket.

    Paginated: a full library is thousands of objects, well past the 1000-key
    cap on one response, and taking page one silently would make the migration
    re-upload everything it could not see.
    """
    sizes: dict[str, int] = {}
    for page in client.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=f"{R2_STEM_PREFIX}/"
    ):
        for item in page.get("Contents", []):
            sizes[item["Key"]] = item["Size"]
    return sizes


__all__ = [
    "DEFAULT_BUCKET",
    "R2_CONTENT_ADDRESSED_LAYOUT",
    "R2_ENV_KEYS",
    "R2_STEM_PREFIX",
    "content_addressed_key",
    "list_r2_sizes",
    "r2_client",
]
