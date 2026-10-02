"""Produce and read the ``karaoke_words`` artifact through the CloudSync tier.

The artifact is the per-track JSON of :mod:`apps.lyrics.karaoke_cache`. It
uses the content-addressed asset tier for STORAGE ONLY: the location record is
``lyric_verdict.words_content_hash``, never a ``track_locations`` row.

Why not :func:`apps.cloud.hydration.apply_policy_after_produce`, which is the
generic producer: it writes a REMOTE ``track_locations`` row unconditionally,
``track_locations`` has no ``asset_kind`` column, and
``hydration_core._content_hash`` takes no ``asset_kind`` either - so an AUDIO
resolve of the track could come back holding the lyrics JSON's digest. In
``cached`` mode that function also moves the file under
``<hash[:2]>/<hash>``, destroying the ``{stable_id}.json`` addressing this
cache is read by. A words artifact must never be able to poison playback, so
this module owns the narrow sequence instead, reusing
:mod:`apps.cloud.asset_store` and :func:`apps.cloud.hydration_core.
resolve_policy` rather than copying either.

``s3`` and ``cfg`` are explicit parameters. The CLI builds them (
``require_credentials`` + ``boto3_asset_client``) only in cloud mode and
passes None in local mode; cloud mode refuses None rather than quietly
degrading to a local-only write.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.cloud import asset_store, hydration_core, policy
from apps.cloud.config import CloudConfig
from apps.cloud.eviction import HydrationError
from apps.lyrics import karaoke_cache, store
from apps.lyrics.tail_sanitize import sanitize_words_for_artifact
from apps.shared.state import sync_stamp

ASSET_KIND: str = "karaoke_words"


def _track_duration_s(conn: sqlite3.Connection, stable_id: str) -> float | None:
    row = conn.execute(
        "SELECT duration_ms FROM tracks WHERE stable_id = ? AND deleted_at IS NULL",
        (stable_id,),
    ).fetchone()
    if row is None:
        return None
    duration_ms = row[0]
    if not isinstance(duration_ms, int) or duration_ms <= 0:
        return None
    return duration_ms / 1000.0


@dataclass(frozen=True)
class WordsArtifact:
    """What a produce left behind, and what the verdict row must record."""

    path: Path
    content_hash: str
    n_words: int
    n_lines: int


def asset_clients_for_mode(
    *, writing: bool
) -> tuple[asset_store.AssetS3Client | None, CloudConfig | None]:
    """``(s3, cfg)`` for the resolved CloudSync mode, for a CLI to pass in.

    Real R2 clients in cloud mode, ``(None, None)`` in local mode where
    nothing may touch the network. Callers pass the pair explicitly rather
    than letting the producer reach for credentials on its own.

    ``writing`` is keyword-only with NO default because "will this run touch
    R2" IS the credential decision. A dry run pushes and deletes nothing, so
    demanding credentials for one would make a rehearsal impossible on a
    laptop that has none; stating it here keeps that judgement in one place
    instead of a ternary at every subcommand.
    """
    if policy.CFG.mode not in ("local", "cloud"):
        raise AssertionError(f"unhandled cloudsync mode {policy.CFG.mode!r}")
    # The two credential-free cases, which share an answer for two different
    # reasons: a dry run pushes and deletes nothing, and local mode may not
    # touch the network at all.
    if not writing or policy.CFG.mode == "local":
        return None, None
    cfg = CloudConfig.from_env()
    asset_store.require_credentials(cfg)
    return asset_store.boto3_asset_client(cfg), cfg


def _require_clients(
    s3: asset_store.AssetS3Client | None, cfg: CloudConfig | None, stable_id: str
) -> tuple[asset_store.AssetS3Client, CloudConfig]:
    if s3 is None or cfg is None:
        raise HydrationError(
            f"cloudsync mode is 'cloud' but no S3 client / CloudConfig was "
            f"supplied for {ASSET_KIND} {stable_id!r}; build them with "
            "asset_store.require_credentials + boto3_asset_client under "
            "`doppler run -p general -c dev_personal --`."
        )
    return s3, cfg


def _publish(
    conn: sqlite3.Connection,
    *,
    stable_id: str,
    path: Path,
    content_hash: str,
    s3: asset_store.AssetS3Client | None,
    cfg: CloudConfig | None,
) -> None:
    """Push the artifact if the resolved policy says so. Cloud mode only.

    A missing ``sync_policies`` row for (machine, karaoke_words) raises
    :class:`HydrationError` out of ``resolve_policy``: not caught, not
    defaulted. The runbook PUTs that cell once per machine.
    """
    client, config = _require_clients(s3, cfg, stable_id)
    machine_id = sync_stamp.ensure_local_machine(conn)
    resolution = hydration_core.resolve_policy(
        conn, stable_id, machine_id, asset_kind=ASSET_KIND
    )
    if resolution.mode == "pinned":
        pushed = asset_store.push_asset(config, client, path)
        if pushed.content_hash != content_hash:
            raise HydrationError(
                f"{ASSET_KIND} {stable_id!r} hashed to {content_hash} locally "
                f"but {pushed.content_hash} on push; the file changed under us."
            )
        if not asset_store.object_exists(config, client, content_hash):
            raise HydrationError(
                f"{ASSET_KIND} {stable_id!r} pushed to {pushed.object_key} but "
                "a HEAD does not find it; refusing to record a hash for an "
                "object that is not durable."
            )
    elif resolution.mode == "excluded":
        return
    elif resolution.mode in ("cached", "stream"):
        raise HydrationError(
            f"policy mode {resolution.mode!r} (from {resolution.source}) is not "
            f"supported for {ASSET_KIND}: the artifact is addressed by "
            "{stable_id}.json and its only location record is "
            "lyric_verdict.words_content_hash, so there is nothing to evict to "
            "or stream from. Use 'pinned' or 'excluded'."
        )
    else:
        raise AssertionError(f"unhandled policy mode {resolution.mode!r}")


def produce_words_artifact(
    conn: sqlite3.Connection,
    *,
    data_dir: Path,
    stable_id: str,
    source: str,
    words: Sequence[Mapping[str, Any]],
    s3: asset_store.AssetS3Client | None,
    cfg: CloudConfig | None,
) -> WordsArtifact:
    """Write one track's words and publish them per the resolved policy.

    Sequence: canonical bytes to the cache path (tmp + rename), hash, then in
    ``local`` mode nothing at all (no network), and in ``cloud`` mode
    ``resolve_policy`` decides - ``pinned`` pushes and verifies, ``excluded``
    keeps only the local file, ``cached`` / ``stream`` are refused. The caller
    records ``content_hash`` on the verdict row; no ``track_locations`` row is
    ever written.
    """
    duration_s = _track_duration_s(conn, stable_id)
    cleaned, _report = sanitize_words_for_artifact(words, duration_s=duration_s)
    built = karaoke_cache.build_words(stable_id=stable_id, source=source, words=cleaned)
    path, content_hash, n_words, n_lines = karaoke_cache.write_words(
        karaoke_cache.cache_path(data_dir, stable_id), built
    )
    mode = policy.CFG.mode
    if mode == "local":
        pass
    elif mode == "cloud":
        _publish(
            conn,
            stable_id=stable_id,
            path=path,
            content_hash=content_hash,
            s3=s3,
            cfg=cfg,
        )
    else:
        raise AssertionError(f"unhandled cloudsync mode {mode!r}")
    return WordsArtifact(
        path=path, content_hash=content_hash, n_words=n_words, n_lines=n_lines
    )


def _parse_verified(path: Path, stable_id: str, expected_hash: str | None) -> (
    karaoke_cache.KaraokeWords
):
    """Parse a local artifact, refusing bytes that are not what the row says.

    A stale or tampered cache is an ERROR, not a silent fallback to the cloud
    copy: if the two disagree, which one is the track's words is exactly the
    question nobody can answer later.
    """
    body = path.read_bytes()
    actual = hashlib.sha256(body).hexdigest()
    if expected_hash is None:
        raise HydrationError(
            f"{path} holds {ASSET_KIND} for {stable_id!r} but the live "
            "lyric_verdict row records no words_content_hash; the file cannot "
            "be trusted and must not be served."
        )
    if actual != expected_hash:
        raise HydrationError(
            f"{path} hashes to {actual} but the lyric_verdict row for "
            f"{stable_id!r} records {expected_hash}; the local cache is stale "
            "or tampered."
        )
    return karaoke_cache.parse_words(body, stable_id)


def load_words(
    conn: sqlite3.Connection,
    *,
    data_dir: Path,
    stable_id: str,
    s3: asset_store.AssetS3Client | None,
    cfg: CloudConfig | None,
) -> karaoke_cache.KaraokeWords | None:
    """This track's words: local file first, then R2 by hash, else None.

    None means "this machine has no words for that track and cannot get any",
    which the PR-4 route turns into a 404 with an explicit detail.
    """
    path = karaoke_cache.cache_path(data_dir, stable_id)
    verdict = store.get_verdict(conn, stable_id)
    expected = verdict.words_content_hash if verdict is not None else None
    if path.is_file():
        return _parse_verified(path, stable_id, expected)
    if expected is None:
        return None
    mode = policy.CFG.mode
    if mode == "local":
        return None
    elif mode == "cloud":  # noqa: RET505 - explicit elif is the house style
        client, config = _require_clients(s3, cfg, stable_id)
        asset_store.fetch_asset(config, client, expected, path)
        return karaoke_cache.parse_words(path, stable_id)
    else:
        raise AssertionError(f"unhandled cloudsync mode {mode!r}")


__all__ = [
    "ASSET_KIND",
    "WordsArtifact",
    "asset_clients_for_mode",
    "load_words",
    "produce_words_artifact",
]
