"""Flag file location and JSON override I/O.

``<data-dir>/feature-flags.json`` is the default on-disk override surface; env
vars and shipped profiles can redirect resolution (see :func:`flags_path`).
Reading is strict: unknown keys and non-boolean values raise :class:`FlagFileError`
rather than being interpreted.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from apps.feature_flags.profiles import profile_path, selected_profile

#: Where the file lives inside the data dir, unless the env var overrides it.
FLAGS_FILENAME: str = "feature-flags.json"

FLAGS_FILE_ENV: str = "MDT_FEATURE_FLAGS_FILE"


class FlagFileError(RuntimeError):
    """The flag file exists but this process will not guess what it meant."""


def flags_path(data_dir: Path | str) -> Path:
    """Where flags are read from, most specific source first.

    1. ``MDT_FEATURE_FLAGS_FILE``  an explicit path, for a lane or a test
    2. ``MDT_BUILD_PROFILE``      a named profile shipped in-repo
    3. ``<data-dir>/feature-flags.json``  the user's own overrides

    2 beats 3 on purpose: a packaged store build must not be re-enabled by a
    file dropped into the data dir. See apps/feature_flags/profiles.py.
    """
    override = os.environ.get(FLAGS_FILE_ENV, "").strip()
    if override:
        return Path(override)
    profile = profile_path(selected_profile())
    if profile is not None:
        return profile
    return Path(data_dir) / FLAGS_FILENAME


def read_overrides(path: Path, declared: frozenset[str]) -> dict[str, bool]:
    """Parse the file, refusing anything ambiguous."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FlagFileError(
            f"{path} is not valid JSON ({exc}). RUNBOOK: fix or delete it. It "
            "is not treated as absent -- a broken flag file must not silently "
            "become 'every flag at its default'."
        ) from exc
    if not isinstance(raw, dict):
        raise FlagFileError(f'{path} must hold a JSON object of {{"flag_id": true|false}}, found {type(raw).__name__}')
    overrides: dict[str, bool] = {}
    for key, value in raw.items():
        if key not in declared:
            raise FlagFileError(
                f"{path} sets unknown flag {key!r}. Declared flags: "
                f"{sorted(declared) or ['<none declared>']}. Add a FlagDef to "
                "apps/feature_flags/store.FLAGS, or remove the key -- an "
                "override nothing reads is a lie about what is switched on."
            )
        if not isinstance(value, bool):
            raise FlagFileError(
                f"{path} sets {key!r} to {value!r}; a flag is true or false, "
                "and this reader will not decide whether a string or a number "
                "meant on or off."
            )
        overrides[key] = value
    return overrides


__all__ = [
    "FLAGS_FILENAME",
    "FLAGS_FILE_ENV",
    "FlagFileError",
    "flags_path",
    "read_overrides",
]
