"""Reconcile a present-library crate between the Mac owner and agentbox.

The owner database is never copied.  Instead the owner writes a stable-ID
ledger from its live database and files, while the replica independently stats
the declared destinations and must reproduce the same digest.  Rsync remains
the transfer engine, so files whose size and mtime already agree are not sent
again.

Usage:
  python -m apps.webui.crate_sync --dry-run --preload1
  python -m apps.webui.crate_sync --live --local --preload1
  python -m apps.webui.crate_sync --live --remote --preload1
  python -m apps.webui.crate_sync --audit --remote
  python -m apps.webui.crate_sync --status
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from apps.shared import fs_residency, library_mode, platform_paths
from apps.shared.crate_index import audit_manifest, ledger_digest
from apps.shared.platform_paths import PROJECT_ROOT
from apps.webui.run_agentbox import (
    AGENTBOX_HOSTNAME,
    allowed_ssh_host,
    allowed_ssh_hosts_description,
    ssh_agentbox_argv,
)
from apps.webui.soft_deletes import has_soft_deletes

# Re-export, not a copy: the one prefix set lives in platform_paths (T3b D1).
STREAMING_PREFIXES: tuple[str, ...] = platform_paths.STREAMING_PREFIXES
PRELOAD1_PRESET: Path = (
    PROJECT_ROOT
    / "apps"
    / "webui"
    / "frontend"
    / "src"
    / "routes"
    / "performance"
    / "preload1"
    / "preset.ts"
)
DEFAULT_CRATE_ROOT: Path = Path("/data/mdt-crate")
#: Env naming this deployment's owner-Mac home prefix(es) (os.pathsep-
#: separated absolute paths, e.g. "/Users/<name>"). No personal name is
#: hardcoded here (#910); unset means the caller must pass --map explicitly.
DEFAULT_USER_PREFIXES_ENV: str = "MDT_OWNER_USER_PREFIXES"
OWNER_MEDIA_SUBTREES: frozenset[str] = frozenset({"Documents", "Music"})
REMOTE_REPO: Path = Path("/root/music-dj-tools")
#: Env naming this deployment's allowed owner-Mac SSH target(s) (user@host or
#: user@tailnet-name, os.pathsep-separated; first entry is the --owner
#: default). No personal tailnet identity is hardcoded here (#910); unset
#: fails fast, only at the point a --remote pull actually needs it, because
#: validating against an empty allowlist would silently refuse everyone
#: while looking like a real check.
OWNER_SSH_TARGETS_ENV: str = "MDT_OWNER_SSH_TARGETS"
#: Env naming this deployment's owner-Mac repo checkout path, used to sanity
#: check the path a --remote pull is told to trust (#910). Unset fails fast
#: the same way, and only when --remote pull needs it.
OWNER_REPO_ENV: str = "MDT_OWNER_REPO"
#: Env naming the private key this deployment's owner-Mac pull lane
#: authenticates with. A path under one machine's /root is deployment config,
#: not repo content, so it sits beside OWNER_SSH_TARGETS_ENV rather than as a
#: literal (#1540). Unset fails fast at the point a pull needs it, the same
#: contract as the two env names above.
OWNER_SSH_KEY_ENV: str = "MDT_OWNER_SSH_KEY"
ANLZ_SIBLING_SUFFIXES: frozenset[str] = frozenset({".DAT", ".EXT", ".2EX"})
ARTWORK_SIBLINGS: tuple[str, ...] = ("artwork.jpg", "artwork_s.jpg", "artwork_m.jpg")
FileKind = Literal["audio", "anlz", "artwork"]
DestKind = Literal["ssh", "local"]


@dataclass(frozen=True)
class CrateFile:
    source: Path
    dest: Path
    kind: FileKind
    size_bytes: int
    mtime_s: int = 0
    stable_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SyncPlan:
    scope: str
    files: tuple[CrateFile, ...]
    skipped_streaming: int
    skipped_absent: int


# ----- preload1 ids -------------------------------------------------------


def preload1_stable_ids(preset_path: Path = PRELOAD1_PRESET) -> tuple[str, ...]:
    """Read the four preload1 deck ids from the TypeScript preset. No copy."""
    if not preset_path.is_file():
        raise RuntimeError(f"preload1 preset missing: {preset_path}")
    ids = tuple(
        re.findall(
            r"stable_id:\s*'([0-9a-f]{40})'", preset_path.read_text(encoding="utf-8")
        )
    )
    if len(ids) != 4:
        raise RuntimeError(
            f"preload1 preset {preset_path} must declare 4 stable_id values, got {len(ids)}"
        )
    return ids


# ----- path map / dest ----------------------------------------------------


def _default_user_prefixes() -> tuple[str, ...]:
    """This deployment's owner-Mac home prefix(es), read fresh from the
    environment (not cached at import time) so a placeholder can never be
    baked into tracked code and a test can set/unset the variable per case.
    """
    raw = os.environ.get(DEFAULT_USER_PREFIXES_ENV, "")
    return tuple(p for p in raw.split(os.pathsep) if p)


def default_user_maps(crate_root: Path) -> tuple[tuple[str, str], ...]:
    prefixes = _default_user_prefixes()
    if not prefixes:
        raise RuntimeError(
            f"{DEFAULT_USER_PREFIXES_ENV} is not set and --map was not given -- "
            "export this machine's owner-Mac home prefix(es) (os.pathsep-"
            "separated absolute paths, e.g. /Users/<name>) or pass --map FROM=TO"
        )
    return tuple(
        (prefix, str(crate_root / "users" / prefix.rsplit("/", 1)[-1]))
        for prefix in prefixes
    )


def _owner_ssh_targets() -> tuple[str, ...]:
    """This deployment's allowed owner-Mac SSH targets, read fresh from the
    environment (not cached at import time) so a test can set/unset the
    variable per case. Raises when empty: an empty allowlist must refuse
    every owner explicitly, not compare true against nothing.
    """
    raw = os.environ.get(OWNER_SSH_TARGETS_ENV, "")
    targets = tuple(t for t in raw.split(os.pathsep) if t)
    if not targets:
        raise RuntimeError(
            f"{OWNER_SSH_TARGETS_ENV} is not set -- export this deployment's "
            "owner-Mac SSH target(s) (user@host, os.pathsep-separated; the "
            "first is used as the --owner default) before a --remote pull"
        )
    return targets


def _owner_ssh_default() -> str:
    return _owner_ssh_targets()[0]


def _owner_repo_default() -> Path:
    """This deployment's owner-Mac repo checkout, read fresh from the
    environment. Raises when unset: a --remote pull with no --owner-repo and
    no configured default must fail loudly rather than trust a placeholder
    that matches nothing real (#910).
    """
    raw = os.environ.get(OWNER_REPO_ENV)
    if not raw:
        raise RuntimeError(
            f"{OWNER_REPO_ENV} is not set and --owner-repo was not given -- "
            "export this deployment's owner-Mac repo checkout path"
        )
    return Path(raw)


def parse_map_entry(raw: str) -> tuple[str, str]:
    """Parse one ``FROM=TO`` map entry, in either OS's absolute syntax.

    ``startswith("/")`` is not what "absolute" means on Windows, where the
    owner tree arrives as ``D:\\music`` or ``\\\\nas\\music``. The shared
    two-syntax test lives in :mod:`apps.shared.platform_paths` so the CLI
    and the on-disk path map agree on what they will accept.
    """
    if "=" not in raw:
        raise argparse.ArgumentTypeError(f"--map must be FROM=TO, got {raw!r}")
    from_prefix, to_prefix = raw.split("=", 1)
    from_prefix = platform_paths.normalise_path_prefix(from_prefix.strip())
    to_prefix = platform_paths.normalise_path_prefix(to_prefix.strip())
    if not from_prefix or not to_prefix:
        raise argparse.ArgumentTypeError(f"--map must be FROM=TO, got {raw!r}")
    if not platform_paths.is_any_absolute(
        from_prefix
    ) or not platform_paths.is_any_absolute(to_prefix):
        raise argparse.ArgumentTypeError(f"--map paths must be absolute, got {raw!r}")
    return from_prefix, to_prefix


def _relative_to_prefix(source: Path, prefix: str) -> Path | None:
    """``source`` under ``prefix``, or ``None`` when it is not under it.

    Compared as paths rather than strings: a Windows prefix carries
    backslashes and a drive letter, which no ``str.startswith`` test against
    ``Path.as_posix()`` will ever match.
    """
    try:
        return source.relative_to(Path(prefix))
    except ValueError:
        return None


def crate_dest(
    source: Path, *, crate_root: Path, user_maps: Sequence[tuple[str, str]]
) -> Path:
    """Map one owner-machine path onto the replica crate. Fail if unmapped."""
    posix = source.as_posix()
    if posix.startswith("/PIONEER/"):
        return crate_root / "pioneer-share" / posix.lstrip("/")
    # Normalised on both sides, and by the same rule ``_source_group`` uses:
    # a share root that only matches after symlink resolution would map here
    # and then trip the mapping-drift check there. Resolving BOTH sides also
    # keeps a `..`-bearing or symlinked source from dodging the share branch
    # and falling through to the user maps.
    share = platform_paths.resolve_local(platform_paths.SHARE_ROOT)
    rel = _relative_to_prefix(platform_paths.resolve_local(source), str(share))
    if rel is not None:
        return crate_root / "pioneer-share" / rel
    for from_prefix, to_prefix in user_maps:
        relative = _relative_to_prefix(source, from_prefix)
        if relative is not None:
            return Path(to_prefix) / relative
    raise RuntimeError(f"no crate mapping for {source}")


def path_map_document(user_maps: Sequence[tuple[str, str]]) -> dict[str, object]:
    return {"entries": [{"from": src, "to": dest} for src, dest in user_maps]}


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


# ----- sqlite -------------------------------------------------------------


def _open_ro(path: Path, label: str) -> sqlite3.Connection:
    if not path.is_file():
        raise RuntimeError(f"{label} missing: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _has_membership_order_key(conn: sqlite3.Connection) -> bool:
    return any(
        row[1] == "order_key"
        for row in conn.execute("PRAGMA table_info(playlist_memberships)")
    )


def _playlist_members_sql(conn: sqlite3.Connection) -> str:
    live = has_soft_deletes(conn, "playlist_memberships")
    keyed = _has_membership_order_key(conn)
    members_sql_live_keyed = (
        "SELECT stable_id FROM playlist_memberships "
        "WHERE playlist_id = ? AND deleted_at IS NULL "
        "ORDER BY COALESCE(order_key, printf('%08d', position)), position"
    )
    members_sql_live_position = (
        "SELECT stable_id FROM playlist_memberships "
        "WHERE playlist_id = ? AND deleted_at IS NULL "
        "ORDER BY position"
    )
    members_sql_keyed = (
        "SELECT stable_id FROM playlist_memberships "
        "WHERE playlist_id = ? "
        "ORDER BY COALESCE(order_key, printf('%08d', position)), position"
    )
    members_sql_position = (
        "SELECT stable_id FROM playlist_memberships "
        "WHERE playlist_id = ? "
        "ORDER BY position"
    )
    if live and keyed:
        return members_sql_live_keyed
    if live:
        return members_sql_live_position
    if keyed:
        return members_sql_keyed
    return members_sql_position


def _playlist_stable_ids(state: sqlite3.Connection, name: str) -> tuple[str, ...]:
    if has_soft_deletes(state, "playlists"):
        lookup_sql = (
            "SELECT playlist_id, name FROM playlists "
            "WHERE name = ? AND deleted_at IS NULL"
        )
    else:
        lookup_sql = (
            "SELECT playlist_id, name FROM playlists "
            "WHERE name = ?"
        )
    rows = state.execute(lookup_sql, (name,)).fetchall()
    if not rows:
        if has_soft_deletes(state, "playlists"):
            known_sql = (
                "SELECT DISTINCT name FROM playlists "
                "WHERE deleted_at IS NULL ORDER BY name"
            )
        else:
            known_sql = (
                "SELECT DISTINCT name FROM playlists "
                "ORDER BY name"
            )
        known = [r[0] for r in state.execute(known_sql)]
        raise RuntimeError(
            f"unknown playlist {name!r}. Known: {', '.join(known) or '(none)'}"
        )
    ids: list[str] = []
    seen: set[str] = set()
    members_sql = _playlist_members_sql(state)
    for playlist_id, _name in rows:
        for (stable_id,) in state.execute(members_sql, (playlist_id,)):
            if stable_id not in seen:
                seen.add(stable_id)
                ids.append(stable_id)
    if not ids:
        raise RuntimeError(f"playlist {name!r} has no members")
    return tuple(ids)


def _selected_stable_ids(
    state: sqlite3.Connection,
    *,
    playlist: str | None,
    stable_ids: Sequence[str] | None,
    preload1: bool,
) -> tuple[str, ...] | None:
    if preload1:
        return preload1_stable_ids()
    if stable_ids is not None:
        return tuple(stable_ids)
    if playlist is not None:
        return _playlist_stable_ids(state, playlist)
    return None


def _scope_name(
    *, playlist: str | None, stable_ids: Sequence[str] | None, preload1: bool
) -> str:
    if preload1:
        return "preload1"
    if playlist is not None:
        return f"playlist:{playlist}"
    if stable_ids is not None:
        return "stable-ids"
    return "present"


def _is_streaming(path: str | None) -> bool:
    return platform_paths.is_streaming_uri(path)


def _materialised_source(raw: str | None) -> Path | None:
    if not raw or _is_streaming(raw):
        return None
    path = Path(raw)
    if fs_residency.is_materialised(path):
        return path
    if raw.startswith("/PIONEER/"):
        share_path = platform_paths.SHARE_ROOT / raw.lstrip("/")
        if fs_residency.is_materialised(share_path):
            return share_path
    return None


def _anlz_sources(analysis_data_path: str | None) -> list[Path]:
    source = _materialised_source(analysis_data_path)
    if source is None:
        return []
    return sorted(
        (
            sibling
            for sibling in source.parent.iterdir()
            if sibling.stem == source.stem
            and sibling.suffix.upper() in ANLZ_SIBLING_SUFFIXES
            and fs_residency.is_materialised(sibling)
        ),
        key=lambda path: path.name,
    )


def _artwork_sources(image_path: str | None) -> list[Path]:
    source = _materialised_source(image_path)
    if source is None:
        return []
    files = [source]
    for name in ARTWORK_SIBLINGS:
        sibling = source.with_name(name)
        if sibling != source and fs_residency.is_materialised(sibling):
            files.append(sibling)
    return files


def collect_plan(
    *,
    state_db: Path,
    master_db: Path | None,
    crate_root: Path,
    user_maps: Sequence[tuple[str, str]],
    playlist: str | None = None,
    stable_ids: Sequence[str] | None = None,
    preload1: bool = False,
) -> SyncPlan:
    """Keep a row only when the owner-machine source file is present."""
    from apps.webui import crate_sync_plan as plan_mod

    state = _open_ro(state_db, "STATE_DB")
    try:
        wanted = _selected_stable_ids(
            state, playlist=playlist, stable_ids=stable_ids, preload1=preload1
        )
        rows = plan_mod.selected_track_rows(state, wanted)
        vendor_ids = {
            str(r["stable_id"]): str(r["vendor_id"])
            for r in state.execute(
                "SELECT stable_id, vendor_id FROM track_vendor_ids WHERE vendor = 'rekordbox'"
            )
        }
    finally:
        state.close()

    rb_assets = plan_mod.load_rb_assets(master_db)
    files, skipped_streaming, skipped_absent = plan_mod.accumulate_plan_files(
        rows,
        vendor_ids,
        rb_assets,
        crate_root,
        user_maps,
    )
    ordered = tuple(sorted(files.values(), key=lambda item: item.dest.as_posix()))
    return SyncPlan(
        scope=_scope_name(playlist=playlist, stable_ids=stable_ids, preload1=preload1),
        files=ordered,
        skipped_streaming=skipped_streaming,
        skipped_absent=skipped_absent,
    )


def plan_bytes(plan: SyncPlan) -> int:
    return sum(item.size_bytes for item in plan.files)


# ----- copy ---------------------------------------------------------------


def _rsync_to_host(source: Path, dest_host: str, dest: Path) -> None:
    parent = dest.parent.as_posix()
    mkdir = ssh_agentbox_argv(f"mkdir -p {shlex.quote(parent)}", ssh_host=dest_host)
    made = subprocess.run(mkdir, check=False, capture_output=True, text=True)
    if made.returncode != 0:
        detail = made.stderr.strip() or made.stdout.strip() or f"exit {made.returncode}"
        raise RuntimeError(f"remote mkdir failed for {parent}: {detail}")
    copied = subprocess.run(
        [
            "rsync",
            "-a",
            str(source),
            f"{dest_host}:{shlex.quote(dest.as_posix())}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if copied.returncode != 0:
        detail = (
            copied.stderr.strip()
            or copied.stdout.strip()
            or f"exit {copied.returncode}"
        )
        raise RuntimeError(f"rsync {source} -> {dest_host}:{dest} failed: {detail}")


class CrateDestinationEscape(RuntimeError):
    """A ``--map`` aimed a copy outside the replica crate.

    ``crate_dest`` returns a user ``--map`` target verbatim, so
    ``--dest local --map /Users/me/Music=<SHARE_ROOT>/Contents`` would
    mkdir + copy2 straight INTO the real Pioneer share. Containment used to be
    enforced on the ssh-PULL lane only; the local-copy and ssh-push lanes both
    took the mapped path as given, which is the only reason this module could
    be allowlisted as "reads SHARE_ROOT, writes the replica crate".
    """


def _assert_within_crate(dest: Path, crate_root: Path, *, local: bool) -> None:
    """Refuse a destination that leaves the replica crate.

    Path comparison in pathlib is LEXICAL. ``Path('/crate/../etc')
    .is_relative_to('/crate')`` is True even though resolving it escapes, and
    ``Path.__eq__`` does not resolve either, so a containment check written on
    unresolved paths is decoration. Two cases, and both sides get the same
    treatment in each:

      * ``local=True``  -- the destination is on this filesystem, so resolve
        BOTH sides. That collapses ``..`` and follows symlinks, which is the
        only way a symlink pointing into the Pioneer share is caught.
      * ``local=False`` -- the destination lives on another host and cannot be
        resolved from here at all. Reject any ``..`` component outright (so
        there is nothing left to collapse) and compare normalised paths. A
        remote symlink is out of reach either way; the ssh forced command is
        what bounds that lane.
    """
    if local:
        candidate = dest.resolve(strict=False)
        root = crate_root.resolve(strict=False)
    else:
        if ".." in dest.parts or ".." in crate_root.parts:
            raise CrateDestinationEscape(
                f"remote crate destination contains '..' and cannot be resolved "
                f"from here: {dest}"
            )
        candidate = Path(os.path.normpath(dest.as_posix()))
        root = Path(os.path.normpath(crate_root.as_posix()))
    if not candidate.is_relative_to(root):
        raise CrateDestinationEscape(
            f"crate destination escapes the replica crate: {dest} resolves to "
            f"{candidate}, which is not under {root}. A --map target must stay "
            "inside --crate-root; no lane may write into the real Pioneer share."
        )


def _copy_local(source: Path, dest: Path) -> bool:
    if dest.is_file():
        source_stat = source.stat()
        dest_stat = dest.stat()
        if source_stat.st_size == dest_stat.st_size and int(
            source_stat.st_mtime
        ) == int(dest_stat.st_mtime):
            return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return True


def _changed_file_count(output: bytes) -> int:
    return sum(1 for raw in output.splitlines() if len(raw) >= 2 and raw[1:2] == b"f")


def _source_group(
    item: CrateFile,
    *,
    crate_root: Path,
    user_maps: Sequence[tuple[str, str]],
    share_root: Path | None = None,
) -> tuple[Path, Path, Path]:
    """Return source root, destination root, and relative file path.

    Roots are normalised with :func:`platform_paths.resolve_local` rather
    than ``Path.resolve``: the owner prefixes are Mac paths, and resolving
    one on Windows would anchor it to the current drive and hand the caller
    back a root (``D:/Users/user``) that names nothing.
    """
    source = platform_paths.resolve_local(item.source)
    pioneer_source_root = platform_paths.resolve_local(
        share_root if share_root is not None else platform_paths.SHARE_ROOT
    )
    try:
        relative = source.relative_to(pioneer_source_root)
    except ValueError:
        relative = None
    if relative is not None:
        dest_root = crate_root / "pioneer-share"
        if dest_root / relative != item.dest:
            raise RuntimeError(
                f"Pioneer crate mapping drift for {item.source}: expected "
                f"{dest_root / relative}, planned {item.dest}"
            )
        return pioneer_source_root, dest_root, relative

    for source_raw, dest_raw in sorted(
        user_maps, key=lambda pair: len(pair[0]), reverse=True
    ):
        source_root = platform_paths.resolve_local(Path(source_raw))
        try:
            relative = source.relative_to(source_root)
        except ValueError:
            continue
        dest_root = Path(dest_raw)
        # Every entry in user_maps names an owner-Mac home by construction
        # -- --map's own contract ("FROM=TO user prefix"), or
        # MDT_OWNER_USER_PREFIXES via default_user_maps when --map was not
        # given. The narrowing below must be derived from that same source
        # of truth (this loop's own user_maps), never from the env var
        # alone (#910/P1): gating on _default_user_prefixes() left the
        # --map path unnarrowed whenever the env var was unset, which is a
        # fully supported configuration. So the only question left is
        # whether this file falls under an approved media subtree; if it
        # does not, the source root would stay the bare home directory,
        # which crate_owner_ssh_gate.ALLOWED_SOURCE_ROOTS never approves --
        # refuse instead of silently widening the rsync source to the
        # whole home.
        if not (relative.parts and relative.parts[0] in OWNER_MEDIA_SUBTREES):
            raise RuntimeError(
                f"refusing bare-home rsync source for {item.source}: "
                f"{source_root} is an owner-home prefix but the file is not "
                f"under an approved subtree {sorted(OWNER_MEDIA_SUBTREES)}; "
                "narrowing could not apply and the source root would be the "
                "whole home directory"
            )
        subtree = relative.parts[0]
        source_root /= subtree
        dest_root /= subtree
        relative = Path(*relative.parts[1:])
        if dest_root / relative != item.dest:
            raise RuntimeError(
                f"crate mapping drift for {item.source}: expected "
                f"{dest_root / relative}, planned {item.dest}"
            )
        return source_root, dest_root, relative

    raise RuntimeError(f"no rsync source group for {item.source}")


def _rsync_plan_to_host(
    plan: SyncPlan,
    *,
    dest_host: str,
    crate_root: Path,
    user_maps: Sequence[tuple[str, str]],
) -> int:
    """Copy a plan in one NUL-safe rsync batch per source root."""
    groups: dict[tuple[Path, Path], list[Path]] = {}
    for item in plan.files:
        # Escape must be refused before any narrowing logic runs, and before
        # any ssh/rsync call: `_source_group` below can raise its own
        # bare-home RuntimeError first for a mapping that never resolves a
        # dest_root at all, which would let an out-of-crate `--map` target
        # slip past this containment gate under a different error.
        _assert_within_crate(item.dest, crate_root, local=False)
        source_root, dest_root, relative = _source_group(
            item, crate_root=crate_root, user_maps=user_maps
        )
        groups.setdefault((source_root, dest_root), []).append(relative)

    changed = 0
    for (source_root, dest_root), relative_paths in sorted(
        groups.items(), key=lambda pair: str(pair[0][1])
    ):
        # Same containment the pull lane has always had. This lane mkdir -p's
        # dest_root on the remote host, so an unchecked --map target could
        # create a tree anywhere the ssh user can write.
        _assert_within_crate(dest_root, crate_root, local=False)
        mkdir = ssh_agentbox_argv(
            f"mkdir -p {shlex.quote(dest_root.as_posix())}",
            ssh_host=dest_host,
        )
        made = subprocess.run(mkdir, check=False, capture_output=True, text=True)
        if made.returncode != 0:
            detail = (
                made.stderr.strip() or made.stdout.strip() or f"exit {made.returncode}"
            )
            raise RuntimeError(f"remote mkdir failed for {dest_root}: {detail}")

        file_list = b"".join(
            relative.as_posix().encode("utf-8") + b"\0"
            for relative in sorted(set(relative_paths))
        )
        copied = subprocess.run(
            [
                "rsync",
                "-a",
                "--relative",
                "--itemize-changes",
                "--out-format=%i\t%n%L",
                "--from0",
                "--files-from=-",
                f"{source_root.as_posix().rstrip('/')}/",
                f"{dest_host}:{shlex.quote(dest_root.as_posix().rstrip('/') + '/')}",
            ],
            input=file_list,
            check=False,
            capture_output=True,
        )
        if copied.returncode != 0:
            detail = (
                copied.stderr.decode("utf-8", errors="replace").strip()
                or copied.stdout.decode("utf-8", errors="replace").strip()
                or f"exit {copied.returncode}"
            )
            raise RuntimeError(
                f"rsync batch {source_root} -> {dest_host}:{dest_root} failed: {detail}"
            )
        changed += _changed_file_count(copied.stdout)
    return changed


def allowed_owner_ssh(owner: str) -> bool:
    return owner in _owner_ssh_targets()


def owner_ssh_key_path() -> Path:
    """The configured owner-Mac pull key, or a hard failure.

    No default: this used to be a literal path under one deployment's /root, and
    defaulting an unset value to a path that exists nowhere is how a pull lane
    reports a missing key as a permission error three layers down.
    """
    raw = os.environ.get(OWNER_SSH_KEY_ENV, "").strip()
    if not raw:
        raise RuntimeError(
            f"{OWNER_SSH_KEY_ENV} must name the owner Mac SSH private key the "
            "crate pull lane authenticates with; unset is not defaulted"
        )
    return Path(raw)


def _require_owner_ssh_key() -> Path:
    """The configured key, proven to exist. Both pull lanes open with this."""
    key = owner_ssh_key_path()
    if not key.is_file():
        raise RuntimeError(f"owner SSH key missing: {key}")
    return key


def ssh_owner_argv(command: str, *, owner: str) -> list[str]:
    if not allowed_owner_ssh(owner):
        raise RuntimeError(
            f"refusing owner SSH target {owner!r}; allowed "
            f"{sorted(_owner_ssh_targets())}"
        )
    key = _require_owner_ssh_key()
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        str(key),
        owner,
        command,
    ]


def _manifest_files(manifest: dict[str, object]) -> list[dict[str, object]]:
    raw = manifest.get("files")
    if not isinstance(raw, list):
        raise TypeError("owner manifest files must be a list")
    entries: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise TypeError("owner manifest file entry must be an object")
        required = {
            "bytes",
            "dest",
            "dest_root",
            "kind",
            "mtime_s",
            "relative",
            "source",
            "source_root",
            "stable_ids",
        }
        missing = required - item.keys()
        if missing:
            raise RuntimeError(
                "owner manifest file entry lacks " + ", ".join(sorted(missing))
            )
        entries.append(item)
    return entries


def _rsync_manifest_from_host(
    manifest: dict[str, object], *, owner: str, crate_root: Path
) -> int:
    """Pull one owner ledger in NUL-safe batches; rsync skips unchanged files."""
    from apps.webui.crate_sync_plan import group_manifest_relatives

    if not allowed_owner_ssh(owner):
        raise RuntimeError(
            f"refusing owner SSH target {owner!r}; allowed "
            f"{sorted(_owner_ssh_targets())}"
        )
    owner_ssh_key = _require_owner_ssh_key()
    groups = group_manifest_relatives(manifest, crate_root)

    changed = 0
    for (source_root, dest_root), relative_paths in sorted(
        groups.items(), key=lambda pair: str(pair[0][1])
    ):
        dest_root.mkdir(parents=True, exist_ok=True)
        file_list = b"".join(
            relative.as_posix().encode("utf-8") + b"\0"
            for relative in sorted(set(relative_paths))
        )
        copied = subprocess.run(
            [
                "rsync",
                "-a",
                "--relative",
                "--itemize-changes",
                "--out-format=%i\t%n%L",
                "--from0",
                "--files-from=-",
                "-e",
                "ssh -o BatchMode=yes -o ConnectTimeout=8 "
                f"-o IdentitiesOnly=yes -i {owner_ssh_key}",
                f"{owner}:{shlex.quote(source_root.as_posix().rstrip('/') + '/')}",
                dest_root.as_posix().rstrip("/") + "/",
            ],
            input=file_list,
            check=False,
            capture_output=True,
        )
        if copied.returncode != 0:
            detail = (
                copied.stderr.decode("utf-8", errors="replace").strip()
                or copied.stdout.decode("utf-8", errors="replace").strip()
                or f"exit {copied.returncode}"
            )
            raise RuntimeError(
                f"rsync pull {owner}:{source_root} -> {dest_root} failed: {detail}"
            )
        changed += _changed_file_count(copied.stdout)
    return changed


def owner_manifest(
    *, owner: str, owner_repo: Path, forwarded_args: Sequence[str]
) -> dict[str, object]:
    expected = _owner_repo_default()
    if owner_repo != expected:
        raise RuntimeError(f"refusing owner repo {owner_repo}; expected {expected}")
    command = shlex.join(["mdt-crate-plan", *forwarded_args])
    result = subprocess.run(
        ssh_owner_argv(command, owner=owner),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (
            result.stderr.strip()
            or result.stdout.strip()
            or f"exit {result.returncode}"
        )
        raise RuntimeError(f"owner plan failed: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"owner plan returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TypeError("owner plan JSON must be an object")
    _manifest_files(payload)
    if "library" in payload:
        from apps.agentbox.crate_state import normalise_manifest

        normalise_manifest(payload)
    return payload


def audit_payload(
    manifest: dict[str, object], *, crate_root: Path, state_db: Path | None = None
) -> dict[str, object]:
    audit = audit_manifest(manifest, crate_root=crate_root)
    payload = audit.as_dict()
    payload["source_host"] = manifest.get("source_host")
    payload["scope"] = manifest.get("scope")
    payload["track_count"] = len(manifest.get("track_files", {}))
    if "library" in manifest:
        if state_db is None:
            raise RuntimeError("library ledger audit requires state.db")
        from apps.agentbox.crate_state import audit_snapshot, snapshot_from_manifest

        library = audit_snapshot(state_db, snapshot_from_manifest(manifest))
        payload["library"] = library
        payload["ok"] = payload["ok"] is True and library["ok"] is True
    return payload


def assert_audit_ok(payload: dict[str, object], *, label: str) -> None:
    if payload.get("ok") is True:
        return
    raise RuntimeError(
        f"{label} ledger mismatch: "
        f"missing={len(payload.get('missing', []))}, "
        f"size={len(payload.get('size_mismatches', []))}, "
        f"mtime={len(payload.get('mtime_mismatches', []))}"
        + (
            ", library=false"
            if isinstance(payload.get("library"), dict)
            and payload["library"].get("ok") is not True
            else ""
        )
    )


def reconcile_library_manifest(
    manifest: dict[str, object], *, state_db: Path, crate_root: Path
) -> dict[str, object]:
    """Apply owner records only on the explicit remote replica."""
    if library_mode.library_mode() != "remote":
        raise RuntimeError(
            "crate library reconciliation requires MDT_LIBRARY_MODE=remote"
        )
    from apps.agentbox.crate_state import (
        normalise_manifest,
        reconcile_snapshot,
        snapshot_from_manifest,
    )

    normalise_manifest(manifest)
    snapshot = snapshot_from_manifest(manifest)
    return reconcile_snapshot(
        state_db,
        snapshot,
        backup_dir=crate_root / "state-backups",
    )


def remote_audit(*, dest_host: str, crate_root: Path) -> dict[str, object]:
    command = f"cd {shlex.quote(str(REMOTE_REPO))} && exec " + shlex.join(
        [
            "uv",
            "run",
            "--no-sync",
            "python",
            "-m",
            "apps.webui.crate_sync",
            "--audit",
            "--remote",
            "--json",
            "--crate-root",
            str(crate_root),
        ]
    )
    result = subprocess.run(
        ssh_agentbox_argv(command, ssh_host=dest_host),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (
            result.stderr.strip()
            or result.stdout.strip()
            or f"exit {result.returncode}"
        )
        raise RuntimeError(f"replica audit failed: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"replica audit returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TypeError("replica audit JSON must be an object")
    assert_audit_ok(payload, label="replica")
    return payload


def remote_reconcile(*, dest_host: str, crate_root: Path) -> dict[str, object]:
    command = f"cd {shlex.quote(str(REMOTE_REPO))} && exec " + shlex.join(
        [
            "uv",
            "run",
            "--no-sync",
            "python",
            "-m",
            "apps.webui.crate_sync",
            "--reconcile-state",
            "--remote",
            "--json",
            "--crate-root",
            str(crate_root),
        ]
    )
    result = subprocess.run(
        ssh_agentbox_argv(command, ssh_host=dest_host),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (
            result.stderr.strip()
            or result.stdout.strip()
            or f"exit {result.returncode}"
        )
        raise RuntimeError(f"replica state reconciliation failed: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"replica state reconciliation returned invalid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise TypeError("replica state reconciliation JSON must be an object")
    return payload


def apply_plan(
    plan: SyncPlan,
    *,
    dest_kind: DestKind,
    dest_host: str,
    crate_root: Path,
    user_maps: Sequence[tuple[str, str]],
) -> int:
    if dest_kind == "ssh":
        return _rsync_plan_to_host(
            plan,
            dest_host=dest_host,
            crate_root=crate_root,
            user_maps=user_maps,
        )
    changed = 0
    for item in plan.files:
        if dest_kind == "local":
            _assert_within_crate(item.dest, crate_root, local=True)
            changed += int(_copy_local(item.source, item.dest))
    return changed


def manifest_payload(
    plan: SyncPlan,
    *,
    source_host: str,
    crate_root: Path,
    user_maps: Sequence[tuple[str, str]],
    spike_verified: bool,
    library_snapshot: dict[str, object],
) -> dict[str, object]:
    files: list[dict[str, object]] = []
    for item in plan.files:
        source_root, dest_root, relative = _source_group(
            item,
            crate_root=crate_root,
            user_maps=user_maps,
        )
        files.append(
            {
                "bytes": item.size_bytes,
                "dest": str(item.dest),
                "dest_root": str(dest_root),
                "kind": item.kind,
                "mtime_s": item.mtime_s,
                "relative": relative.as_posix(),
                "source": str(item.source),
                "source_root": str(source_root),
                "stable_ids": list(item.stable_ids),
            }
        )
    track_files: dict[str, str] = {}
    for item in plan.files:
        if item.kind != "audio":
            continue
        for stable_id in item.stable_ids:
            prior = track_files.setdefault(stable_id, str(item.dest))
            if prior != str(item.dest):
                raise RuntimeError(
                    f"stable_id {stable_id} maps to two crate audio files: "
                    f"{prior} and {item.dest}"
                )
    from apps.agentbox.crate_state import snapshot_digest

    return {
        "bytes": plan_bytes(plan),
        "count": len(plan.files),
        "crate_root": str(crate_root),
        "files": files,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ledger_digest": ledger_digest(files),
        "library": library_snapshot,
        "library_digest": snapshot_digest(library_snapshot),
        "schema_version": 3,
        "scope": plan.scope,
        "skipped_absent": plan.skipped_absent,
        "skipped_streaming": plan.skipped_streaming,
        "source_host": source_host,
        "spike_verified": spike_verified,
        "track_files": track_files,
    }


def load_manifest(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"manifest {path} must be a JSON object")
    return raw


def load_destination_manifest(
    *, dest_kind: DestKind, dest_host: str, manifest_path: Path
) -> dict[str, object]:
    if dest_kind == "local":
        return load_manifest(manifest_path)
    quoted = shlex.quote(manifest_path.as_posix())
    command = f"test ! -f {quoted} || cat -- {quoted}"
    completed = subprocess.run(
        ssh_agentbox_argv(command, ssh_host=dest_host),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = (
            completed.stderr.strip()
            or completed.stdout.strip()
            or f"exit {completed.returncode}"
        )
        raise RuntimeError(f"could not read destination manifest: {detail}")
    if not completed.stdout.strip():
        return {}
    try:
        raw = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"destination manifest {manifest_path} is malformed: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise RuntimeError(
            f"destination manifest {manifest_path} must be a JSON object"
        )
    return raw


def spike_verified(manifest: dict[str, object]) -> bool:
    return bool(manifest.get("spike_verified"))


def assert_full_allowed(*, filtered: bool, spike_ok: bool) -> None:
    if filtered or spike_ok:
        return
    raise RuntimeError(
        "full crate push refused until a spike verify is recorded in "
        "manifest.json (run --preload1 --live, then --verify-spike)"
    )


def assert_push_host(*, dest_kind: DestKind, dest_host: str, to_explicit: bool) -> None:
    """Live copy runs on the Air. The box is the replica, never the source."""
    if dest_kind == "local":
        return
    if sys.platform == "darwin":
        # Exact membership of the configured allowlist, and nothing else. This
        # destination goes to mkdir/rsync directly, without the remote-hostname
        # check run_via_ssh performs, so the allowlist is the only thing between
        # a typo (or a hostile argument) and a library copied off this machine.
        if not allowed_ssh_host(dest_host):
            raise RuntimeError(
                f"refusing dest host {dest_host!r}; allowed {allowed_ssh_hosts_description()}"
            )
        return
    if to_explicit:
        raise RuntimeError(
            "crate push copies FROM the Air (darwin). This host is "
            f"{socket.gethostname()} / {sys.platform}."
        )
    raise RuntimeError(
        "crate push copies FROM the Air (darwin). This host is "
        f"{socket.gethostname()} / {sys.platform}. Status is the box-side command."
    )


# ----- status / verify ----------------------------------------------------


def status_report(
    *,
    state_db: Path,
    crate_root: Path,
    user_maps: Sequence[tuple[str, str]],
    manifest_path: Path,
) -> dict[str, object]:
    mode = library_mode.library_mode()
    manifest = load_manifest(manifest_path)
    if not user_maps:
        raise RuntimeError(
            "status_report requires at least one user_maps entry to probe; "
            f"got none (check {DEFAULT_USER_PREFIXES_ENV} or pass --map)"
        )
    # Probes the FIRST configured prefix rather than a hardcoded placeholder
    # (#910): a literal example path can never equal whatever prefix this
    # deployment actually configured, so "mapped_sample" would report a
    # permanent, misleading non-match even when the real mapping works.
    sample_src = f"{user_maps[0][0]}/Music/track.mp3"
    mapped = platform_paths.resolve_library_path(
        sample_src,
        path_map=platform_paths.PathMap(entries=tuple(user_maps)),
    )
    resolved = 0
    total = 0
    if state_db.is_file():
        state = _open_ro(state_db, "STATE_DB")
        try:
            if has_soft_deletes(state, "tracks"):
                paths_sql = "SELECT file_path FROM tracks WHERE deleted_at IS NULL"
            else:
                paths_sql = "SELECT file_path FROM tracks"
            rows = state.execute(paths_sql).fetchall()
        finally:
            state.close()
        path_map = platform_paths.PathMap(entries=tuple(user_maps))
        for (file_path,) in rows:
            if not file_path or _is_streaming(file_path):
                continue
            total += 1
            result = platform_paths.resolve_asset_path(file_path, path_map=path_map)
            if result.resolved is not None and fs_residency.is_materialised(
                result.resolved
            ):
                resolved += 1
    return {
        "crate_root": str(crate_root),
        "crate_root_exists": crate_root.is_dir(),
        "file_paths_present": total,
        "file_paths_resolved": resolved,
        "library_mode": mode,
        "mapped_sample": None if mapped.resolved is None else str(mapped.resolved),
        "mapped_sample_reason": mapped.reason,
        "manifest_scope": manifest.get("scope"),
        "spike_verified": spike_verified(manifest),
    }


def verify_spike(
    *,
    stable_ids: Sequence[str],
    base_url: str,
) -> dict[str, object]:
    """GET audio + anlz for each spike id. Real HTTP, no mocked status."""
    performance_status = _http_status(f"{base_url.rstrip('/')}/performance/preload1")
    results: list[dict[str, object]] = []
    for stable_id in stable_ids:
        audio_url = f"{base_url.rstrip('/')}/api/v1/tracks/{stable_id}/audio"
        anlz_url = f"{base_url.rstrip('/')}/api/v1/tracks/{stable_id}/anlz?points=100"
        audio_status = _http_status(audio_url)
        anlz_status, anlz_body = _http_json(anlz_url)
        anlz_ok = anlz_status == 200 and "ANALYSIS_NOT_FOUND" not in anlz_body
        results.append(
            {
                "stable_id": stable_id,
                "audio_status": audio_status,
                "anlz_status": anlz_status,
                "anlz_ok": anlz_ok,
            }
        )
    all_ok = performance_status == 200 and all(
        item["audio_status"] == 200 and item["anlz_ok"] is True for item in results
    )
    return {
        "ok": all_ok,
        "performance_status": performance_status,
        "tracks": results,
    }


def _http_status(url: str) -> int:
    request = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(request) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except urllib.error.URLError as exc:
        raise RuntimeError(f"spike verify could not reach {url}: {exc}") from exc


def _http_json(url: str) -> tuple[int, str]:
    request = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(request) as response:
            return int(response.status), response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return int(exc.code), body
    except urllib.error.URLError as exc:
        raise RuntimeError(f"spike verify could not reach {url}: {exc}") from exc


# ----- CLI ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m apps.webui.crate_sync")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run", action="store_true", help="print count + bytes, copy nothing"
    )
    mode.add_argument(
        "--live", action="store_true", help="copy the selected present files"
    )
    mode.add_argument(
        "--status", action="store_true", help="box-side mode / crate / resolve counts"
    )
    mode.add_argument(
        "--audit",
        action="store_true",
        help="independently stat the replica and reconcile it with manifest.json",
    )
    mode.add_argument(
        "--verify-spike",
        action="store_true",
        help="GET audio+anlz for the spike set and record spike_verified",
    )
    mode.add_argument(
        "--reconcile-state",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    filt = parser.add_mutually_exclusive_group()
    filt.add_argument("--playlist", default=None, help="spike one playlist by name")
    filt.add_argument(
        "--stable-ids",
        default=None,
        help="comma-separated stable_id list",
    )
    filt.add_argument(
        "--preload1", action="store_true", help="spike the four preload1 deck ids"
    )
    side = parser.add_mutually_exclusive_group()
    side.add_argument(
        "--local",
        action="store_true",
        help="run on the owner Mac; live mode pushes to agentbox",
    )
    side.add_argument(
        "--remote",
        action="store_true",
        help="run on agentbox; dry/live modes obtain the owner plan over SSH and pull",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="select every currently-present row (refused until spike_verified)",
    )
    parser.add_argument(
        "--to", default=AGENTBOX_HOSTNAME, help="rsync destination host"
    )
    parser.add_argument(
        "--owner",
        default=None,
        help=(
            "fixed SSH owner used only by --remote pull mode; defaults to "
            f"the first entry of {OWNER_SSH_TARGETS_ENV}"
        ),
    )
    parser.add_argument(
        "--owner-repo",
        default=None,
        help=(
            "owner checkout used only by --remote pull mode; defaults to "
            f"{OWNER_REPO_ENV}"
        ),
    )
    parser.add_argument(
        "--dest",
        choices=("ssh", "local"),
        default=None,
        help="ssh (Air -> agentbox) or local copy into --crate-root (tests)",
    )
    parser.add_argument("--crate-root", default=None, help="replica crate root")
    parser.add_argument(
        "--data-dir", default=None, help="owner data dir (state.db + path-map.json)"
    )
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        type=parse_map_entry,
        help=(
            "FROM=TO user prefix (repeatable). Default: derived from "
            f"{DEFAULT_USER_PREFIXES_ENV}"
        ),
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:9400",
        help="Vite/Serve origin for --verify-spike",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit one machine-readable object instead of the human plan",
    )
    return parser


def _stable_ids_arg(raw: str | None) -> tuple[str, ...] | None:
    if raw is None:
        return None
    ids = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not ids:
        raise RuntimeError("--stable-ids was set but named no id")
    return ids


def _owner_library_snapshot(
    state_db: Path,
    *,
    playlist: str | None,
    stable_ids: Sequence[str] | None,
    preload1: bool,
) -> dict[str, object]:
    from apps.agentbox.crate_state import build_snapshot

    selected = preload1_stable_ids() if preload1 else stable_ids
    return build_snapshot(
        state_db,
        playlist_name=playlist,
        stable_ids=selected,
    )


def _filtered(args: argparse.Namespace) -> bool:
    return bool(args.playlist or args.stable_ids or args.preload1)


def _operation_side(args: argparse.Namespace) -> Literal["local", "remote"]:
    if args.local:
        return "local"
    if args.remote:
        return "remote"
    return "remote" if library_mode.library_mode() == "remote" else "local"


def _forwarded_plan_args(
    args: argparse.Namespace,
    *,
    crate_root: Path,
    user_maps: Sequence[tuple[str, str]],
) -> list[str]:
    forwarded = ["--crate-root", str(crate_root)]
    for source, destination in user_maps:
        forwarded.extend(["--map", f"{source}={destination}"])
    if args.playlist:
        forwarded.extend(["--playlist", args.playlist])
    elif args.stable_ids:
        forwarded.extend(["--stable-ids", args.stable_ids])
    elif args.preload1:
        forwarded.append("--preload1")
    if args.full:
        forwarded.append("--full")
    return forwarded


def _print_manifest_plan(payload: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return
    summary = {
        key: payload.get(key)
        for key in (
            "bytes",
            "count",
            "crate_root",
            "ledger_digest",
            "scope",
            "skipped_absent",
            "skipped_streaming",
        )
    }
    summary["track_count"] = len(payload.get("track_files", {}))
    sys.stdout.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    for item in _manifest_files(payload):
        sys.stdout.write(
            f"{item['kind']}\t{item['bytes']}\t{item['source']} -> {item['dest']}\n"
        )


def _run(argv: Sequence[str] | None = None) -> int:
    from apps.webui.crate_sync_cli import run_crate_sync

    return run_crate_sync(argv)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return _run(argv)
    except (RuntimeError, TypeError, ValueError) as exc:
        sys.stderr.write(f"crate-sync: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
