"""REACT-01: the React deck twins sit behind ``ui.react.deck``, default OFF.

[if] nothing overrides the flag [then] it resolves OFF (Svelte deck), [else stop].
[if] feature-flags.json sets it on [then] it resolves ON (React twins), [else stop].
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.feature_flags import FLAGS, load_flags

pytestmark = pytest.mark.requirement("REACT-01")

FLAG_ID = "ui.react.deck"


def test_the_flag_is_declared_off_by_default_and_not_sandbox_gated() -> None:
    [flag] = [f for f in FLAGS if f.flag_id == FLAG_ID]
    assert flag.default is False
    assert flag.sandbox_gated is False
    assert flag.owner == "maintainer"
    assert flag.retire_by == "2027-03-01"


def test_with_no_override_it_resolves_off(tmp_path: Path) -> None:
    store = load_flags(tmp_path)
    assert store.enabled(FLAG_ID) is False


def test_a_flag_file_turns_it_on(tmp_path: Path) -> None:
    # Control for the test above: the same read can say yes, so its "off"
    # is the default speaking, not a store that cannot see the flag.
    (tmp_path / "feature-flags.json").write_text(json.dumps({FLAG_ID: True}))
    store = load_flags(tmp_path)
    assert store.enabled(FLAG_ID) is True
