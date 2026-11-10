"""Unit tests for the flags HTTP wire mappers in ``apps.feature_flags.http``."""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.feature_flags import load_flags
from apps.feature_flags.http import flag_out, flags_out_from_store, refusal_out
from apps.feature_flags.profiles import BUILD_PROFILE_ENV, DEFAULT_PROFILE, STORE_PROFILE
from tests.feature_flags.support import clear_flag_selection_env


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_flag_selection_env(monkeypatch)


def test_flag_out_copies_store_fields_without_refusal(tmp_path: Path) -> None:
    store = load_flags(tmp_path)
    flag = next(item for item in store.snapshot() if item.flag_id == "usb.export")
    out = flag_out(flag, from_store_profile=False, sandboxed=False)
    assert out.flag_id == flag.flag_id
    assert out.enabled == flag.enabled
    assert out.default == flag.default
    assert out.overridden == flag.overridden
    assert out.owner == flag.owner
    assert out.note == flag.note
    assert out.retire_by == flag.retire_by
    assert out.refusal is None


def test_refusal_out_none_when_store_profile_did_not_turn_flag_off(tmp_path: Path) -> None:
    store = load_flags(tmp_path)
    flag = next(item for item in store.snapshot() if item.flag_id == "usb.export")
    assert refusal_out(flag, from_store_profile=False, sandboxed=False) is None


def test_flags_out_from_store_matches_full_build(tmp_path: Path) -> None:
    store = load_flags(tmp_path)
    payload = flags_out_from_store(store, sandboxed=False)
    assert payload.path == str(store.path)
    assert payload.file_present is store.file_present
    assert payload.build_profile == DEFAULT_PROFILE
    assert payload.sandboxed is False
    assert len(payload.flags) == len(store.snapshot())


def test_flags_out_from_store_carries_refusal_on_store_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BUILD_PROFILE_ENV, STORE_PROFILE)
    store = load_flags(tmp_path)
    payload = flags_out_from_store(store, sandboxed=False)
    assert payload.build_profile == STORE_PROFILE
    usb = next(item for item in payload.flags if item.flag_id == "usb.export")
    assert usb.enabled is False
    assert usb.refusal is not None
    assert usb.refusal.code
