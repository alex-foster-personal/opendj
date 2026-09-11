"""LIBM-41: refuse a scan that would wipe a previously populated root.

A briefly unmounted volume, an empty mount point, or a permission error all
present as "zero files". That is an ERROR, not a measurement. Callers pass
the count they just resolved and the count they last recorded; this module
never writes availability or ``deleted_at`` itself.

Default drop fraction is 50%. Override only with an explicit
``allow_mass_missing`` (the ``--allow-mass-missing`` CLI flag).
"""
from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

DEFAULT_DROP_FRACTION: float = 0.5


class MassMissingError(RuntimeError):
    """A scan root dropped too far from its last recorded count."""

    def __init__(
        self,
        root: str,
        prior_n: int,
        current_n: int,
        *,
        drop_fraction: float = DEFAULT_DROP_FRACTION,
    ) -> None:
        self.root = root
        self.prior_n = prior_n
        self.current_n = current_n
        self.drop_fraction = drop_fraction
        percent = int(drop_fraction * 100)
        super().__init__(
            f"scan root {root} previously resolved {prior_n} files, now "
            f"{current_n}; refusing to mark missing (LIBM-41). Pass "
            f"--allow-mass-missing to override a drop of more than {percent}%."
        )


def path_is_under_root(path: str | Path, root: str | Path) -> bool:
    """True when ``path`` is ``root`` or a descendant. Does not resolve.

    Scan paths often no longer exist (unmounted volume), so inode identity
    is not available. Prefix match on the normalized form is the answer.
    """
    path_s = os.path.normpath(str(path))
    root_s = os.path.normpath(str(root))
    if path_s == root_s:
        return True
    return path_s.startswith(root_s + os.sep)


def guard_scan_count(
    root: str | Path,
    current_n: int,
    prior_n: int | None,
    *,
    drop_fraction: float = DEFAULT_DROP_FRACTION,
    allow_mass_missing: bool = False,
) -> None:
    """Refuse a scan that collapses a populated root.

    ``prior_n is None`` (or 0) means the root is genuinely new: zero is
    accepted and becomes the baseline. ``current_n == 0`` after ``prior_n > 0``
    is always a refusal. A drop of more than ``drop_fraction`` is the same
    refusal. ``allow_mass_missing`` is the only override.
    """
    if allow_mass_missing:
        return
    if prior_n is None or prior_n <= 0:
        return
    if current_n == 0 or (prior_n - current_n) / prior_n > drop_fraction:
        raise MassMissingError(
            str(root),
            prior_n,
            current_n,
            drop_fraction=drop_fraction,
        )


def guard_roots(
    roots: Iterable[Path | str],
    current_paths: Iterable[str],
    prior_paths: Iterable[str],
    *,
    drop_fraction: float = DEFAULT_DROP_FRACTION,
    allow_mass_missing: bool = False,
) -> None:
    """Apply :func:`guard_scan_count` once per scan root."""
    current = list(current_paths)
    prior = list(prior_paths)
    for root in roots:
        current_n = sum(1 for path in current if path_is_under_root(path, root))
        prior_n = sum(1 for path in prior if path_is_under_root(path, root))
        guard_scan_count(
            root,
            current_n,
            prior_n if prior_n else None,
            drop_fraction=drop_fraction,
            allow_mass_missing=allow_mass_missing,
        )


__all__ = [
    "DEFAULT_DROP_FRACTION",
    "MassMissingError",
    "guard_roots",
    "guard_scan_count",
    "path_is_under_root",
]
