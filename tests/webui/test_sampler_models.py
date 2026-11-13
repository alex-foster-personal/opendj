"""Edge sampler wire models: slot settings defaults and request body validation.

[if] a wire model accepts a body the spec refuses or its defaults drift [then] these tests fail, [else stop].

Regression one-liners:
  - if default_settings drifts from DEFAULT_SETTINGS in sampler-types.ts then broken
  - if a patch accepts an over-long name, an out-of-range setting or an unknown key then broken
  - if a set name over 40 characters is accepted then broken
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from apps.webui.server import sampler_models as models


def test_if_a_slot_has_no_stored_settings_then_the_defaults_follow_the_client() -> None:
    """[if] defaults are asked for a loop or a one-shot [then] they match sampler-types.ts, [else stop]."""
    loop = models.default_settings(is_loop=True)
    assert loop == {
        "launch": "bar",
        "key": "auto",
        "tempo": "match",
        "mode": "loop",
        "repeat": "loop",
        "downbeat_offset": 0,
        "level": 80,
    }
    assert models.default_settings(is_loop=False)["repeat"] == "once"


@pytest.mark.parametrize(
    "body",
    [
        {"name": "x" * 61},
        {"description": "x" * 201},
        {"settings": {**models.default_settings(is_loop=True), "level": 101}},
        {"settings": {**models.default_settings(is_loop=True), "downbeat_offset": 4}},
        {"colour": "red"},
    ],
)
def test_if_a_patch_is_over_long_out_of_range_or_unknown_then_it_is_refused(body: dict[str, Any]) -> None:
    """[if] a patch has an over-long text, an out-of-range setting or an extra key [then] it fails, [else stop]."""
    with pytest.raises(ValidationError):
        models.SlotPatch.model_validate(body)


def test_if_a_set_name_is_over_40_chars_then_it_is_refused() -> None:
    """[if] a set name is 41 characters [then] SetPut refuses it, [else stop]."""
    with pytest.raises(ValidationError):
        models.SetPut.model_validate({"name": "x" * 41, "description": "", "slot_ids": None})
