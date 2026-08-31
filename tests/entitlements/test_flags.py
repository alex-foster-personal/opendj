"""The local feature-flag file: offline, loud, and not an entitlement store.

A flag system fails in exactly one interesting way -- silently. A typo'd key
sits in a file doing nothing while somebody swears the flag is on; an
undeclared flag read as "off by default" disables a branch permanently and
cannot be found by grep. Every assertion here is against one of those.

Regression lines:
  - if a missing flag file is anything other than "every flag at its default"
    then a fresh checkout does not boot
  - if a malformed flag file is treated as absent then a broken file silently
    becomes the default set
  - if an undeclared key in the file is accepted then an override nobody
    reads lies about what is switched on
  - if enabled() answers False for an undeclared flag then a typo disables a
    code path forever
  - if flag resolution performs any network call then FLAG-02 is broken and
    the app has stopped being local-first
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from apps.feature_flags import (
    FLAGS,
    FLAGS_FILE_ENV,
    FLAGS_FILENAME,
    FlagDef,
    FlagFileError,
    flags_path,
    load_flags,
)

#: A registry that exists only here. Production declares no flags (there is no
#: real one to declare), and inventing one so the tests have something to read
#: would be exactly the fabricated state the house rules ban.
TEST_DEFS: tuple[FlagDef, ...] = (
    FlagDef(
        flag_id="example.off_by_default",
        default=False,
        owner="tests",
        note="exists only inside this module",
        retire_by="never - test fixture",
    ),
    FlagDef(
        flag_id="example.on_by_default",
        default=True,
        owner="tests",
        note="exists only inside this module",
        retire_by="never - test fixture",
    ),
)


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FLAGS_FILE_ENV, raising=False)


def _write(data_dir: Path, payload: object) -> Path:
    path = data_dir / FLAGS_FILENAME
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ----- FLAG-01: a separate store from entitlements ------------------------
def test_flags_and_entitlements_share_no_module_and_no_file(
    tmp_path: Path,
) -> None:
    import apps.entitlements as entitlements_pkg
    import apps.feature_flags as flags_pkg

    assert flags_pkg.__name__ != entitlements_pkg.__name__
    # The entitlement seam exposes has/quota and nothing flag-shaped; the flag
    # store exposes enabled and nothing entitlement-shaped. If either grows
    # the other's vocabulary the two concepts have started merging.
    assert not hasattr(flags_pkg, "has")
    assert not hasattr(flags_pkg, "quota")
    assert not hasattr(entitlements_pkg, "load_flags")
    assert not hasattr(entitlements_pkg, "FlagStore")
    # And no shared file: entitlements read an env var, flags read this path.
    assert flags_path(tmp_path).name == FLAGS_FILENAME


# ----- the shipped state --------------------------------------------------
def test_production_declares_no_flags_yet() -> None:
    # Not a placeholder assertion: shipping a fabricated flag so the mechanism
    # "does something" is the mocked state the house rules ban. Delete this
    # line when the first REAL flag is declared.
    assert FLAGS == ()


def test_missing_file_means_every_flag_at_its_default(tmp_path: Path) -> None:
    store = load_flags(tmp_path, defs=TEST_DEFS)
    assert store.file_present is False
    assert store.enabled("example.off_by_default") is False
    assert store.enabled("example.on_by_default") is True
    assert all(not flag.overridden for flag in store.snapshot())


def test_an_override_wins_and_says_it_did(tmp_path: Path) -> None:
    _write(tmp_path, {"example.off_by_default": True})
    store = load_flags(tmp_path, defs=TEST_DEFS)
    assert store.file_present is True
    assert store.enabled("example.off_by_default") is True
    by_id = {flag.flag_id: flag for flag in store.snapshot()}
    assert by_id["example.off_by_default"].overridden is True
    assert by_id["example.off_by_default"].default is False
    # Untouched flags must not be reported as overridden by association.
    assert by_id["example.on_by_default"].overridden is False


def test_the_env_var_moves_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "lane" / "flags.json"
    elsewhere.parent.mkdir()
    elsewhere.write_text(json.dumps({"example.on_by_default": False}), "utf-8")
    monkeypatch.setenv(FLAGS_FILE_ENV, str(elsewhere))
    store = load_flags(tmp_path, defs=TEST_DEFS)
    assert store.path == elsewhere
    assert store.enabled("example.on_by_default") is False


# ----- every ambiguous file is refused ------------------------------------
def test_invalid_json_raises_rather_than_reading_as_absent(
    tmp_path: Path,
) -> None:
    (tmp_path / FLAGS_FILENAME).write_text("{ not json", encoding="utf-8")
    with pytest.raises(FlagFileError) as excinfo:
        load_flags(tmp_path, defs=TEST_DEFS)
    assert "RUNBOOK" in str(excinfo.value)


@pytest.mark.parametrize("payload", [[], "on", 3])
def test_a_non_object_file_is_refused(tmp_path: Path, payload: object) -> None:
    _write(tmp_path, payload)
    with pytest.raises(FlagFileError):
        load_flags(tmp_path, defs=TEST_DEFS)


def test_an_undeclared_key_in_the_file_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, {"example.typoo": True})
    with pytest.raises(FlagFileError) as excinfo:
        load_flags(tmp_path, defs=TEST_DEFS)
    assert "example.typoo" in str(excinfo.value)


@pytest.mark.parametrize("value", ["true", 1, 0, None, "yes"])
def test_a_non_boolean_value_is_refused(tmp_path: Path, value: object) -> None:
    _write(tmp_path, {"example.off_by_default": value})
    with pytest.raises(FlagFileError):
        load_flags(tmp_path, defs=TEST_DEFS)


def test_reading_an_undeclared_flag_raises_rather_than_answering_false(
    tmp_path: Path,
) -> None:
    store = load_flags(tmp_path, defs=TEST_DEFS)
    with pytest.raises(KeyError) as excinfo:
        store.enabled("example.never_declared")
    assert "example.never_declared" in str(excinfo.value)


# ----- FLAG-02: offline, by construction ----------------------------------
def test_flag_resolution_performs_no_network_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sockets are made to explode, then flags are resolved anyway.

    Stronger than reading the source for an http client: this fails for ANY
    route to the network, including one a future dependency drags in.
    """

    def _no_sockets(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "flag resolution opened a socket; FLAG-02 requires flags to "
            "resolve fully offline because the daemon is local-first"
        )

    monkeypatch.setattr(socket, "socket", _no_sockets)
    monkeypatch.setattr(socket, "create_connection", _no_sockets)

    _write(tmp_path, {"example.on_by_default": False})
    store = load_flags(tmp_path, defs=TEST_DEFS)
    assert store.enabled("example.on_by_default") is False
    assert store.snapshot()[0].flag_id == "example.off_by_default"
