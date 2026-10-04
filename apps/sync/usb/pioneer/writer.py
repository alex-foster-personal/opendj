"""Runnable alias for the OneLibrary writer CLI.

``python -m apps.sync.usb.pioneer.writer --help`` is forwarded to the
package-level ``write`` subcommand by prepending ``write`` to argv. This
keeps backward-compatible discoverability for callers who treat the
writer as its own entry point, without duplicating the argparse tree.

See :mod:`apps.sync.usb.pioneer.__main__` for the full CLI, and
:mod:`apps.sync.usb.pioneer.writer_onelibrary` for the underlying
:func:`write_onelibrary` API.

Requirement: CAT-06.
"""
from __future__ import annotations

import sys

from .__main__ import main as _package_main


def main(argv: list[str] | None = None) -> int:
    """Entry point: forward to the package-level ``write`` subcommand."""
    args = list(sys.argv[1:] if argv is None else argv)
    # Handle --help / -h at the top level by forwarding to ``write -h``
    # so users see the write-specific help and not the package help.
    if not args or args[0] in {"-h", "--help"}:
        forward = ["write", "--help"] if args else ["write", "--help"]
        # ``argparse`` exits via SystemExit on --help; let it bubble up.
        return _package_main(forward)
    return _package_main(["write", *args])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
