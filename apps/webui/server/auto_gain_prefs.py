"""The ``auto_gain`` UI pref (AUTOGAIN-05): on/off and the target loudness.

Stored in ``ui-prefs.json`` beside the other prefs and served by
``GET/PUT /api/v1/ui-prefs``. Kept here so the ui_prefs route only wires it.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool

from apps.analysis_loudness.http import auto_gain

DEFAULT_AUTO_GAIN: dict[str, Any] = {
    "enabled": auto_gain.DEFAULT_ENABLED,
    "target_lufs": auto_gain.DEFAULT_TARGET_LUFS,
}


class AutoGainPrefsOut(BaseModel):
    """Auto gain: match every track to ``target_lufs`` (integrated, BS.1770)."""

    model_config = ConfigDict(frozen=True)

    enabled: StrictBool = auto_gain.DEFAULT_ENABLED
    target_lufs: float = auto_gain.DEFAULT_TARGET_LUFS


def _invalid(message: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": "UI_PREFS_INVALID", "message": message})


def parse_auto_gain(raw: Any) -> dict[str, Any]:
    """Validate a stored or merged ``auto_gain`` object. Missing keys take defaults."""
    if raw is None:
        return dict(DEFAULT_AUTO_GAIN)
    if not isinstance(raw, dict):
        raise _invalid("auto_gain must be an object")
    out = dict(DEFAULT_AUTO_GAIN)
    if "enabled" in raw:
        if not isinstance(raw["enabled"], bool):
            raise _invalid("auto_gain.enabled must be a boolean")
        out["enabled"] = raw["enabled"]
    if "target_lufs" in raw:
        target = raw["target_lufs"]
        if isinstance(target, bool) or not isinstance(target, (int, float)):
            raise _invalid("auto_gain.target_lufs must be a number")
        try:
            out["target_lufs"] = auto_gain.validate_target_lufs(float(target))
        except ValueError as exc:
            raise _invalid(str(exc)) from exc
    return out


def merge_auto_gain(current: dict[str, Any], patch: AutoGainPrefsOut | None) -> None:
    """Merge the named fields of a PUT onto what is stored."""
    if patch is None:
        return
    current["auto_gain"] = parse_auto_gain(
        {**current.get("auto_gain", DEFAULT_AUTO_GAIN), **patch.model_dump(exclude_unset=True)}
    )
