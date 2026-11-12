"""Owner path mapping and rsync source-root grouping for crate sync.

Pioneer-share vs user-map resolution, ``--map`` parsing, and ``_source_group``
live here so :mod:`apps.webui.crate_sync` stays under the file-size gate
without rewriting transfer logic (issue #11225).
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from apps.shared import platform_paths

DEFAULT_USER_PREFIXES_ENV: str = "MDT_OWNER_USER_PREFIXES"
OWNER_MEDIA_SUBTREES: frozenset[str] = frozenset({"Documents", "Music"})


class _RsyncSourceItem(Protocol):
    @property
    def source(self) -> Path: ...
    @property
    def dest(self) -> Path: ...


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
    return tuple((prefix, str(crate_root / "users" / prefix.rsplit("/", 1)[-1])) for prefix in prefixes)


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
    if not platform_paths.is_any_absolute(from_prefix) or not platform_paths.is_any_absolute(to_prefix):
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


def crate_dest(source: Path, *, crate_root: Path, user_maps: Sequence[tuple[str, str]]) -> Path:
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


def _source_group(
    item: _RsyncSourceItem,
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
                f"Pioneer crate mapping drift for {item.source}: expected {dest_root / relative}, planned {item.dest}"
            )
        return pioneer_source_root, dest_root, relative

    for source_raw, dest_raw in sorted(user_maps, key=lambda pair: len(pair[0]), reverse=True):
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
                f"crate mapping drift for {item.source}: expected {dest_root / relative}, planned {item.dest}"
            )
        return source_root, dest_root, relative

    raise RuntimeError(f"no rsync source group for {item.source}")
