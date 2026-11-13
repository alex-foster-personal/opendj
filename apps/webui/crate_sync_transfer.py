"""Copy lanes for crate sync: local copy, ssh push and owner pull.

Split out of :mod:`apps.webui.crate_sync` (issue #9687); ``crate_sync``
re-exports every name here. Every lane refuses a destination outside the
replica crate through ``_assert_within_crate``.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from apps.webui.crate_sync_model import DestKind, SyncPlan
from apps.webui.crate_sync_owner import _owner_ssh_targets, _require_owner_ssh_key, allowed_owner_ssh
from apps.webui.crate_sync_paths import _source_group
from apps.webui.run_agentbox import ssh_agentbox_argv

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
        detail = copied.stderr.strip() or copied.stdout.strip() or f"exit {copied.returncode}"
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
                f"remote crate destination contains '..' and cannot be resolved from here: {dest}"
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
        if source_stat.st_size == dest_stat.st_size and int(source_stat.st_mtime) == int(dest_stat.st_mtime):
            return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return True


def _changed_file_count(output: bytes) -> int:
    return sum(1 for raw in output.splitlines() if len(raw) >= 2 and raw[1:2] == b"f")


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
        source_root, dest_root, relative = _source_group(item, crate_root=crate_root, user_maps=user_maps)
        groups.setdefault((source_root, dest_root), []).append(relative)

    changed = 0
    for (source_root, dest_root), relative_paths in sorted(groups.items(), key=lambda pair: str(pair[0][1])):
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
            detail = made.stderr.strip() or made.stdout.strip() or f"exit {made.returncode}"
            raise RuntimeError(f"remote mkdir failed for {dest_root}: {detail}")

        file_list = b"".join(relative.as_posix().encode("utf-8") + b"\0" for relative in sorted(set(relative_paths)))
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
            raise RuntimeError(f"rsync batch {source_root} -> {dest_host}:{dest_root} failed: {detail}")
        changed += _changed_file_count(copied.stdout)
    return changed


def _rsync_manifest_from_host(manifest: dict[str, object], *, owner: str, crate_root: Path) -> int:
    """Pull one owner ledger in NUL-safe batches; rsync skips unchanged files."""
    from apps.webui.crate_sync_plan import group_manifest_relatives

    if not allowed_owner_ssh(owner):
        raise RuntimeError(f"refusing owner SSH target {owner!r}; allowed {sorted(_owner_ssh_targets())}")
    owner_ssh_key = _require_owner_ssh_key()
    groups = group_manifest_relatives(manifest, crate_root)

    changed = 0
    for (source_root, dest_root), relative_paths in sorted(groups.items(), key=lambda pair: str(pair[0][1])):
        dest_root.mkdir(parents=True, exist_ok=True)
        file_list = b"".join(relative.as_posix().encode("utf-8") + b"\0" for relative in sorted(set(relative_paths)))
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
                f"ssh -o BatchMode=yes -o ConnectTimeout=8 -o IdentitiesOnly=yes -i {owner_ssh_key}",
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
            raise RuntimeError(f"rsync pull {owner}:{source_root} -> {dest_root} failed: {detail}")
        changed += _changed_file_count(copied.stdout)
    return changed


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
