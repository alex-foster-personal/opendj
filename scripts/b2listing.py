#!/usr/bin/env python3
"""Parsers for bifrost2's remote directory listings.

These parsers expect POSIX ls output, not a Windows cmd directory table.
The configured store shell must provide that format; see scripts/b2store.py.

The oracle is ``ls -la --time-style=long-iso``, whose columns are fixed and
locale-independent (unlike bare ``ls -l``, whose date column changes shape for
files older than six months). Both the Modal farm and the R2 sync jobs need to
read it, and they cannot share the farm's copy: ``scripts/modal_vocal_farm.py``
imports ``modal`` at module scope, which a standalone sync script has no
business requiring. Hence one stdlib-only home.

-Claude
"""
from __future__ import annotations

import re

# Group 1 is the type flag ('d' for a directory), 2 the byte count, 3 the name.
# The name is captured greedily so that spaces inside it survive.
_LS_LINE = re.compile(
    r"^([-dlbcps])\S{9,}\s+\d+\s+\S+\s+\S+\s+(\d+)\s+"
    r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+(.+?)\s*$"
)


def remote_dirs(listing: str) -> set[str]:
    """Subdirectory names in an `ls -la --time-style=long-iso` listing.

    Excludes . and .. so a caller can treat the result as "what is stored
    here", not "what rows did ls print".
    """
    names = {
        match.group(3)
        for match in map(_LS_LINE.match, listing.splitlines())
        if match and match.group(1) == "d"
    }
    return names - {".", ".."}


def remote_sizes(listing: str) -> dict[str, int]:
    """{filename: bytes} for the regular files in an `ls -la` listing.

    Directory rows are dropped: their reported size is filesystem bookkeeping,
    not content, so including them would make a bundle-vs-bundle size
    comparison fail for a reason that has nothing to do with the transfer.
    """
    sizes: dict[str, int] = {}
    for line in listing.splitlines():
        match = _LS_LINE.match(line)
        if match and match.group(1) == "-":
            sizes[match.group(3)] = int(match.group(2))
    return sizes
