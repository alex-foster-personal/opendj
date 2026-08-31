"""Named build profiles: a shipped flag file selected by NAME, not by path.

WHY A NAME AND NOT A PATH.  The App Store build differs from the normal build
by exactly one thing: which flags are off.  Expressing that as
``MDT_FEATURE_FLAGS_FILE=/some/long/path/inside/the/bundle.json`` works, but it
puts a filesystem path into a build script, a Tauri resource lookup and a
launch argument, and any of the three can drift into pointing at a file that
does not exist -- which the loader would read as "no overrides", i.e. the FULL
build, silently.  A name cannot drift: an unknown one raises here, listing what
is available.

PRECEDENCE, most specific first::

    1. MDT_FEATURE_FLAGS_FILE   an explicit path, for a lane or a test
    2. --build-profile / MDT_BUILD_PROFILE   a named profile, shipped in-repo
    3. <data-dir>/feature-flags.json         the user's own overrides

1 beats 2 so a developer can always point at a scratch file without editing a
profile, and 2 beats 3 so a packaged store build cannot be re-enabled by a
file dropped in the data dir.  That last one is the security-shaped reason the
order is this way round rather than the other.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Selects a named profile. Lower precedence than an explicit file path.
BUILD_PROFILE_ENV: str = "MDT_BUILD_PROFILE"

PROFILES_DIR: Path = Path(__file__).resolve().parent / "profiles"

#: The name meaning "no profile, ship everything". Not a file: the full build
#: is the ABSENCE of overrides, and giving it an empty json file would create
#: a second way to spell the default that could silently disagree.
DEFAULT_PROFILE: str = "full"


class UnknownProfileError(ValueError):
    """A build profile was named that does not exist."""


def available_profiles() -> tuple[str, ...]:
    """Every selectable profile name, including :data:`DEFAULT_PROFILE`."""
    shipped = sorted(p.stem for p in PROFILES_DIR.glob("*.json"))
    return (DEFAULT_PROFILE, *shipped)


def profile_path(name: str) -> Path | None:
    """The flag file for ``name``, or None for the full build.

    Raises rather than falling back, because the fallback would be the full
    build: a typo in a packaging script would ship USB export enabled in a
    sandboxed bundle where it cannot work, and nothing would say so.
    """
    if name == DEFAULT_PROFILE:
        return None
    path = PROFILES_DIR / f"{name}.json"
    if not path.is_file():
        raise UnknownProfileError(
            f"unknown build profile {name!r}. Available: "
            f"{', '.join(available_profiles())}. A profile is a file in "
            f"{PROFILES_DIR}; this is refused rather than defaulted because "
            "the default is the FULL build, and shipping that by accident is "
            "how a store bundle ends up offering a feature the sandbox "
            "forbids."
        )
    return path


def selected_profile(environ: dict[str, str] | None = None) -> str:
    """The profile named by the environment, or :data:`DEFAULT_PROFILE`."""
    env = os.environ if environ is None else environ
    return env.get(BUILD_PROFILE_ENV, "").strip() or DEFAULT_PROFILE


__all__ = [
    "BUILD_PROFILE_ENV",
    "DEFAULT_PROFILE",
    "PROFILES_DIR",
    "UnknownProfileError",
    "available_profiles",
    "profile_path",
    "selected_profile",
]
