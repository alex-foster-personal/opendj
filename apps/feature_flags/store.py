"""Read the flag file, once, at startup.

THE FILE.  ``<data-dir>/feature-flags.json``, a flat JSON object of
``{"<flag_id>": true|false}``.  Override the location with
``MDT_FEATURE_FLAGS_FILE`` (a lane, a test, a packaged build pointing at a
read-only default).  No file at all is the NORMAL state: flags are opt-in
overrides, so every declared flag simply sits at its declared default.

FLAGS ARE DECLARED IN CODE, NOT IN THE FILE.  :data:`FLAGS` is the registry,
and the file may only OVERRIDE what is declared there.  That is what makes
every failure loud:

  * a key in the file that no :class:`FlagDef` declares  -> :class:`FlagFileError`
    (otherwise ``"enable_fast_pathh": true`` sits in a file doing nothing,
    forever, while somebody swears the flag is on);
  * a non-boolean value                                  -> :class:`FlagFileError`
    (``"1"`` is not ``true``, and reading it as either is a guess);
  * ``enabled("typo")`` at a call site                   -> ``KeyError``
    (a flag nobody declared must not read as "off by default", which would
    disable a code path silently and permanently).

WHY THE REGISTRY IS EMPTY.  There is no flag today.  Shipping a fabricated one
so the file "does something" would be exactly the mocked state the house rules
ban.  The mechanism is here, under test through an injected registry, and the
first real flag is one :class:`FlagDef` away.

READ AT STARTUP, NOT PER CALL.  ``apps.engine_core.app.create_app`` calls
:func:`load_flags` once and mounts the store on ``app.state``.  A flag is a
deploy-time decision; re-reading the file per request would make it a runtime
one and let a half-written file change behaviour mid-request.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

#: Where the file lives inside the data dir, unless the env var overrides it.
FLAGS_FILENAME: str = "feature-flags.json"

FLAGS_FILE_ENV: str = "MDT_FEATURE_FLAGS_FILE"


@dataclass(frozen=True)
class FlagDef:
    """One declared flag.

    ``owner`` and ``retire_by`` are required because a flag is TEMPORARY by
    definition.  A flag with no owner and no end date is how a codebase ends
    up with forty of them and two live branches per flag.
    """

    flag_id: str
    default: bool
    owner: str
    note: str
    retire_by: str


@dataclass(frozen=True)
class FlagState:
    """A resolved flag, as the HTTP surface reports it."""

    flag_id: str
    enabled: bool
    default: bool
    overridden: bool
    owner: str
    note: str
    retire_by: str


#: Every declared flag. Empty on purpose; see the module docstring.
FLAGS: tuple[FlagDef, ...] = ()


class FlagFileError(RuntimeError):
    """The flag file exists but this process will not guess what it meant."""


def flags_path(data_dir: Path | str) -> Path:
    """Where flags are read from. The env var wins, explicitly."""
    override = os.environ.get(FLAGS_FILE_ENV, "").strip()
    if override:
        return Path(override)
    return Path(data_dir) / FLAGS_FILENAME


class FlagStore:
    """The resolved flag set for this process.

    Immutable after construction: the whole point of "read at startup" is that
    every call in one boot gets the same answer.
    """

    def __init__(
        self,
        *,
        defs: tuple[FlagDef, ...],
        overrides: dict[str, bool],
        path: Path,
        file_present: bool,
    ) -> None:
        self._defs: dict[str, FlagDef] = {
            definition.flag_id: definition for definition in defs
        }
        self._overrides = dict(overrides)
        self._path = path
        self._file_present = file_present

    @property
    def path(self) -> Path:
        """The file this store was resolved from, present or not."""
        return self._path

    @property
    def file_present(self) -> bool:
        """False when every flag is sitting at its declared default."""
        return self._file_present

    def enabled(self, flag_id: str) -> bool:
        """Is this code path on?

        ``KeyError`` for an undeclared flag, never False. A typo that reads as
        "off" disables a branch silently and is unfindable by grep.
        """
        definition = self._defs.get(flag_id)
        if definition is None:
            declared = sorted(self._defs) or ["<none declared>"]
            raise KeyError(
                f"undeclared feature flag {flag_id!r}: add a FlagDef to "
                f"apps/feature_flags/store.FLAGS before reading it. Declared: "
                f"{declared}"
            )
        return self._overrides.get(flag_id, definition.default)

    def snapshot(self) -> tuple[FlagState, ...]:
        """Every declared flag and where its value came from."""
        return tuple(
            FlagState(
                flag_id=definition.flag_id,
                enabled=self._overrides.get(
                    definition.flag_id, definition.default
                ),
                default=definition.default,
                overridden=definition.flag_id in self._overrides,
                owner=definition.owner,
                note=definition.note,
                retire_by=definition.retire_by,
            )
            for definition in self._defs.values()
        )


def _read_overrides(path: Path, declared: frozenset[str]) -> dict[str, bool]:
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
        raise FlagFileError(
            f"{path} must hold a JSON object of "
            f'{{"flag_id": true|false}}, found {type(raw).__name__}'
        )
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


def load_flags(
    data_dir: Path | str, *, defs: tuple[FlagDef, ...] = FLAGS
) -> FlagStore:
    """Resolve the flag set for this boot. No network, ever (FLAG-02).

    ``defs`` is injectable so the loader is testable without fabricating a
    production flag nothing uses.
    """
    path = flags_path(data_dir)
    declared = frozenset(definition.flag_id for definition in defs)
    file_present = path.is_file()
    overrides = _read_overrides(path, declared) if file_present else {}
    return FlagStore(
        defs=defs,
        overrides=overrides,
        path=path,
        file_present=file_present,
    )


__all__ = [
    "FLAGS",
    "FLAGS_FILENAME",
    "FLAGS_FILE_ENV",
    "FlagDef",
    "FlagFileError",
    "FlagState",
    "FlagStore",
    "flags_path",
    "load_flags",
]
