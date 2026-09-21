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

from apps.feature_flags.profiles import STORE_PROFILE, profile_path, selected_profile
from apps.shared.sandbox import (
    STORE_BUILD_REFUSAL_CODE,
    STORE_BUILD_REFUSAL_TITLE,
    store_build_refusal_message,
)

#: Where the file lives inside the data dir, unless the env var overrides it.
FLAGS_FILENAME: str = "feature-flags.json"

FLAGS_FILE_ENV: str = "MDT_FEATURE_FLAGS_FILE"


@dataclass(frozen=True)
class FlagDef:
    """One declared flag.

    ``owner`` and ``retire_by`` are required because a flag is TEMPORARY by
    definition.  A flag with no owner and no end date is how a codebase ends
    up with forty of them and two live branches per flag.

    ``sandbox_gated`` is required, not defaulted, for the same reason: it is a
    claim about the capability ("the macOS App Sandbox actually forbids
    this"), not a convenience, and a silent False for every future flag would
    let a genuinely sandbox-broken capability ship with no runtime refusal
    (SAND-04) merely because nobody remembered to opt it in.
    """

    flag_id: str
    default: bool
    owner: str
    note: str
    retire_by: str
    sandbox_gated: bool


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
    sandbox_gated: bool


#: Every declared flag. Empty on purpose; see the module docstring.
FLAGS: tuple[FlagDef, ...] = (
    FlagDef(
        flag_id="usb.export",
        default=True,
        owner="maintainer",
        note=(
            "USB export and drive discovery. ON everywhere except the Mac App "
            "Store build, which selects the 'appstore' profile (see "
            "apps/feature_flags/profiles/) turning it off: a sandboxed "
            "process cannot list /Volumes at all, so the "
            "feature cannot work there and must not appear to. Turning the "
            "flag off is what makes the store build a CONFIG of this build "
            "rather than a fork of it. See SAND-02 in "
            "specs/appstore-sandbox-remediation.md."
        ),
        retire_by="2027-03-01",
        sandbox_gated=True,
    ),
    FlagDef(
        flag_id="local_stems.executor",
        default=True,
        owner="maintainer",
        note=(
            "On-device stem separation for local-only CloudSync policy "
            "(issue #1866). OFF hides the path with an explicit refusal; "
            "never a silent no-op."
        ),
        retire_by="2027-03-01",
        sandbox_gated=False,
    ),
)

APP_MODE_IDS: tuple[str, ...] = (
    "performance",
    "library-management",
    "library",
    "music-player",
)

#: Per-mode in-scope flag ids (PERFMODE-07 / issue #2041).
#: Performance keeps the current surface flags. Unbuilt modes are empty
#: until those contracts exist. Membership is not availability: APP_MODES
#: rows stay available:false regardless of this table.
APP_MODE_FEATURE_FLAGS: dict[str, frozenset[str]] = {
    "performance": frozenset({"usb.export", "local_stems.executor"}),
    "library-management": frozenset(),
    "library": frozenset(),
    "music-player": frozenset(),
}


def _check_mode_feature_flags(
    *,
    defs: tuple[FlagDef, ...] = FLAGS,
    table: dict[str, frozenset[str]] = APP_MODE_FEATURE_FLAGS,
) -> None:
    declared = {definition.flag_id for definition in defs}
    if set(table) != set(APP_MODE_IDS):
        raise KeyError(
            f"APP_MODE_FEATURE_FLAGS keys {sorted(table)} must equal "
            f"APP_MODE_IDS {list(APP_MODE_IDS)}"
        )
    for _mode_id, flag_ids in table.items():
        unknown = sorted(flag_ids - declared)
        if unknown:
            raise KeyError(
                f"undeclared feature flag {unknown[0]!r}: add a FlagDef to "
                "apps.feature_flags.store.FLAGS before reading it. Declared: "
                f"{sorted(declared)}"
            )


_check_mode_feature_flags()


@dataclass(frozen=True)
class FlagRefusal:
    """The fourth refusal shape (SAND-01), independent of any wire model.

    Lives beside :class:`FlagState` rather than in ``apps.engine_core.account.api``
    so a route enforcing a flag (a USB gate) and a route disclosing it
    (``/api/v1/flags``) compute the SAME refusal from the SAME inputs instead
    of each guessing at attribution on its own (SAND-01 review, PR #1668).
    """

    code: str
    message: str
    ui_title: str


def store_build_refusal(
    flag: FlagState, *, from_store_profile: bool, sandboxed: bool
) -> FlagRefusal | None:
    """The fourth refusal for a flag the sandbox blocks, else None.

    Two independent paths to the SAME refusal (SAND-04): the shipped App
    Store profile turned the flag off (a CONFIG fact, checked below), or this
    process is genuinely inside the sandbox right now regardless of what the
    flag says (a RUNTIME fact, checked first). The second path exists because
    a mis-packaged bundle can ship the FULL profile -- so the flag reads
    "on" -- while still running inside Apple's sandbox, and the capability is
    exactly as dead either way; deriving the refusal from ``flag.enabled``
    alone would report the panel as fully available while the daemon's own
    USB routes 503.

    Returns ``None`` for every other reason a flag can be off (a plain local
    override, an explicit ``MDT_FEATURE_FLAGS_FILE``): a caller must not
    blame Apple's sandbox for a decision nobody but this machine made.
    """
    if flag.sandbox_gated and sandboxed:
        return FlagRefusal(
            code=STORE_BUILD_REFUSAL_CODE,
            message=store_build_refusal_message(
                flag.flag_id,
                because=(
                    "this process is running inside the macOS App Sandbox, "
                    "which does not permit it."
                ),
            ),
            ui_title=STORE_BUILD_REFUSAL_TITLE,
        )
    if flag.enabled or not flag.overridden or not from_store_profile:
        return None
    return FlagRefusal(
        code=STORE_BUILD_REFUSAL_CODE,
        message=store_build_refusal_message(
            flag.flag_id,
            because=(
                "the macOS App Sandbox does not permit it, so the "
                f"{STORE_PROFILE!r} build profile ships with this flag off."
            ),
        ),
        ui_title=STORE_BUILD_REFUSAL_TITLE,
    )


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

    def enabled_for_mode(self, mode_id: str, flag_id: str) -> bool:
        """Is this flag in-scope for the mode and on for this process?

        ``KeyError`` for an undeclared flag (via ``enabled``) or mode id.
        A declared flag missing from the mode's set returns ``False``.
        """
        process_on = self.enabled(flag_id)
        try:
            in_scope = APP_MODE_FEATURE_FLAGS[mode_id]
        except KeyError:
            raise KeyError(
                f"undeclared app mode {mode_id!r}: must be one of "
                f"{list(APP_MODE_IDS)}"
            ) from None
        if flag_id not in in_scope:
            return False
        return process_on

    def state(self, flag_id: str) -> FlagState:
        """The resolved state of one declared flag. ``KeyError`` if undeclared."""
        definition = self._defs.get(flag_id)
        if definition is None:
            declared = sorted(self._defs) or ["<none declared>"]
            raise KeyError(
                f"undeclared feature flag {flag_id!r}: add a FlagDef to "
                f"apps/feature_flags/store.FLAGS before reading it. Declared: "
                f"{declared}"
            )
        return FlagState(
            flag_id=definition.flag_id,
            enabled=self._overrides.get(definition.flag_id, definition.default),
            default=definition.default,
            overridden=definition.flag_id in self._overrides,
            owner=definition.owner,
            note=definition.note,
            retire_by=definition.retire_by,
            sandbox_gated=definition.sandbox_gated,
        )

    def snapshot(self) -> tuple[FlagState, ...]:
        """Every declared flag and where its value came from."""
        return tuple(self.state(flag_id) for flag_id in self._defs)


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


def store_profile_is_source(store: FlagStore) -> bool:
    """Did the shipped App Store profile supply this process's flag values?

    THREE conditions, and each rules out a way of blaming Apple for something
    else. The profile must be the store one BY NAME, because the SAND-01
    sentence names Apple's sandbox and a future non-store profile would make
    that a fresh lie in the same shape. The resolved file must actually BE
    that profile's file, because ``MDT_FEATURE_FLAGS_FILE`` beats a named
    profile (see apps/feature_flags/profiles.py) and a lane pointing at a
    scratch file is not a store build. And the caller checks ``overridden``
    per flag, so a flag merely sitting at an off default is not attributed to
    the profile either.

    Lives here rather than in ``apps.engine_core.account.api`` so any future
    reader of the flag store can attribute a value to the shipped profile
    without duplicating this reasoning.
    """
    if selected_profile() != STORE_PROFILE:
        return False
    return Path(store.path) == profile_path(STORE_PROFILE)


__all__ = [
    "APP_MODE_FEATURE_FLAGS",
    "APP_MODE_IDS",
    "FLAGS",
    "FLAGS_FILENAME",
    "FLAGS_FILE_ENV",
    "FlagDef",
    "FlagFileError",
    "FlagRefusal",
    "FlagState",
    "FlagStore",
    "flags_path",
    "load_flags",
    "store_build_refusal",
    "store_profile_is_source",
]
