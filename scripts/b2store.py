#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""SSH/SCP asset store CLI using an operator-configured destination.

The legacy alias is ``bifrost2``; override it with MDT_B2STORE_SSH_HOST.
The account and network address are deployment configuration in SSH settings.
The store root remains D:/asset-store for compatibility. No reachable host or
available storage is implied by these defaults.

Remote commands require a POSIX shell with Windows fsutil available for the
free-space query. Paths use forward slashes and shell quoting because the
remote shell interprets backslashes as escapes.

Subcommands (see Store methods): save, get, ls, rm and df.
Every remote call raises on non-zero exit; no silent fallback is provided.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import PurePosixPath

#----- config -----------------------------------------------------------------

#: ssh alias for the store host. The account it resolves to and the tailnet
#: address behind it are deployment config and live in ~/.ssh/config, not here
#: (#1540). Override when a different alias is configured.
SSH_HOST: str = os.environ.get("MDT_B2STORE_SSH_HOST", "bifrost2")
STORE_ROOT: str = "D:/asset-store"         # forward slashes required by the configured POSIX shell
SSH_OPTS: list[str] = ["-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15"]
# long-iso keeps the date column a fixed width regardless of file age or
# locale, which is what makes scripts/b2listing.py's parser stable.
LS_FLAGS: str = "-la --time-style=long-iso"


class RemotePathMissing(RuntimeError):
    """A remote path does not exist.

    A distinct type lets callers distinguish absent remote data from other
    command failures without depending on a Windows cmd error message.
    """


#----- helpers ----------------------------------------------------------------

def _remote_path(remote_rel: str) -> str:
    """Join a store-relative path onto STORE_ROOT, blocking traversal escapes."""
    rel = PurePosixPath(remote_rel.strip("/"))
    if ".." in rel.parts:
        raise ValueError(f"'..' not allowed in remote path: {remote_rel!r}")
    if "\\" in remote_rel:
        raise ValueError(
            f"backslash in remote path {remote_rel!r}: bifrost2 runs bash, "
            "which consumes backslashes as escapes. Use forward slashes."
        )
    return f"{STORE_ROOT}/{rel}" if rel.parts else STORE_ROOT


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a command, streaming nothing, capturing output, raising on failure."""
    done = subprocess.run(argv, text=True, capture_output=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(
            f"command failed ({done.returncode}): {' '.join(argv)}\n{done.stderr.strip()}"
        )
    return done


def _ssh(remote_cmd: str) -> str:
    """Run a POSIX shell one-liner on bifrost2, return stdout."""
    try:
        return _run(["ssh", *SSH_OPTS, SSH_HOST, remote_cmd]).stdout
    except RuntimeError as exc:
        if "No such file or directory" in str(exc):
            raise RemotePathMissing(str(exc)) from exc
        raise


#----- store ------------------------------------------------------------------

class Store:
    """Thin scp/ssh wrapper over the bifrost2 asset store at STORE_ROOT."""

    def save(self, local_path: str, remote_subdir: str = "") -> str:
        """Push a local file/dir into the store; returns the remote path."""
        src = local_path.rstrip("/")
        name = PurePosixPath(src).name
        dest_dir = _remote_path(remote_subdir)
        remote_target = f"{dest_dir}/{name}"
        # `mkdir -p` is idempotent, which removes the TOCTOU race the old
        # `if not exist X mkdir X` had: two publishers could both see the dir
        # missing, both mkdir, and the loser exit non-zero.
        _ssh(f"mkdir -p {shlex.quote(dest_dir)}")
        _run(["scp", *SSH_OPTS, "-r", src, f"{SSH_HOST}:{remote_target}"])
        return remote_target

    def get(self, remote_rel: str, local_dir: str = ".") -> str:
        """Pull a stored path back to local_dir; returns the local path."""
        remote_target = _remote_path(remote_rel)
        _run(["scp", *SSH_OPTS, "-r", f"{SSH_HOST}:{remote_target}", local_dir])
        return str(PurePosixPath(local_dir) / PurePosixPath(remote_rel).name)

    def ls(self, remote_rel: str = "") -> str:
        """List the store (or a subpath). Raises RemotePathMissing if absent."""
        return _ssh(f"ls {LS_FLAGS} {shlex.quote(_remote_path(remote_rel))}")

    def rm(self, remote_rel: str) -> str:
        """Delete a stored file/dir. Refuses to remove the store root."""
        target = _remote_path(remote_rel)
        if target == STORE_ROOT:
            raise ValueError("refusing to rm the store root")
        return _ssh(f"rm -rf {shlex.quote(target)}")

    def df(self) -> str:
        """Remote free space on the store drive."""
        return _ssh("fsutil volume diskfree D:")


#----- cli --------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="b2store", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_save = sub.add_parser("save", help="push a local file/dir into the store")
    p_save.add_argument("local_path")
    p_save.add_argument("remote_subdir", nargs="?", default="")

    p_get = sub.add_parser("get", help="pull a stored path back to the Mac")
    p_get.add_argument("remote_rel")
    p_get.add_argument("local_dir", nargs="?", default=".")

    p_ls = sub.add_parser("ls", help="list the store (or a subpath)")
    p_ls.add_argument("remote_rel", nargs="?", default="")

    p_rm = sub.add_parser("rm", help="delete a stored path (refuses the root)")
    p_rm.add_argument("remote_rel")

    sub.add_parser("df", help="remote free space on the store drive")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    store = Store()
    if args.cmd == "save":
        print(store.save(args.local_path, args.remote_subdir))
    elif args.cmd == "get":
        print(store.get(args.remote_rel, args.local_dir))
    elif args.cmd == "ls":
        print(store.ls(args.remote_rel))
    elif args.cmd == "rm":
        print(store.rm(args.remote_rel) or f"removed {args.remote_rel}")
    elif args.cmd == "df":
        print(store.df())
    else:  # argparse required=True makes this unreachable
        raise ValueError(f"unhandled cmd: {args.cmd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
