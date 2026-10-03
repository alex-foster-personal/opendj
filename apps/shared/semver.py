"""SemVer 2.0 precedence shared by publication and engine update checks.

Supersedes: core-only comparison in scripts.release_semver.Semver and
apps.engine_core.update_channel._Version. Build metadata does not change
precedence; numeric prerelease identifiers compare numerically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

_SEMVER = re.compile(
    r"(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\.(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


@total_ordering
@dataclass(frozen=True)
class Semver:
    """A version's precedence, excluding build metadata by specification."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Semver):
            return NotImplemented
        here = (self.major, self.minor, self.patch)
        there = (other.major, other.minor, other.patch)
        if here != there:
            return here < there
        if not self.prerelease:
            return False
        if not other.prerelease:
            return True
        for left, right in zip(self.prerelease, other.prerelease, strict=False):
            if left == right:
                continue
            left_numeric, right_numeric = left.isdigit(), right.isdigit()
            if left_numeric and right_numeric:
                return int(left) < int(right)
            if left_numeric != right_numeric:
                return left_numeric
            return left < right
        return len(self.prerelease) < len(other.prerelease)


def parse_semver(raw: str) -> Semver:
    """Read strict SemVer, preserving the existing leading-v compatibility."""
    candidate = raw.strip()
    if candidate.startswith("v"):
        candidate = candidate[1:]
    matched = _SEMVER.fullmatch(candidate)
    if matched is None:
        raise ValueError(f"{raw!r} is not a semver version")
    prerelease = tuple(matched["pre"].split(".")) if matched["pre"] else ()
    if any(identifier.isdigit() and len(identifier) > 1 and identifier[0] == "0" for identifier in prerelease):
        raise ValueError(f"{raw!r} has a numeric semver prerelease identifier with a leading zero")
    return Semver(int(matched["major"]), int(matched["minor"]), int(matched["patch"]), prerelease)
