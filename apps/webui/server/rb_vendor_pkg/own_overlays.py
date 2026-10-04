"""Every own-lane overlay, in the order `/anlz` applies them.

`anlz.py` calls ONE function after its cache read so the call site does not
grow a line per lane (it sits at the 600-line per-file ceiling, which is why
the beatgrid overlay was split out of it in the first place). Each overlay
resolves its own lane's effective source and is a no-op when that lane is
rekordbox, so composing them is order-independent in behavior; the order below
is the order they are written in, which is what a reader checking one against
the other needs.

A new own lane (waveform, loudness) chains in here, not at the call site.
Own cues (CUES-01) ride the same chain: not a selectable lane, but the same
"own record wins when present" rule.

-Claude
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .own_beatgrid_overlay import apply_own_beatgrid
from .own_cues_overlay import apply_own_cues
from .own_key_overlay import apply_own_key_segments
from .own_waveform_overlay import apply_own_waveform


def apply_own_overlays(
    payload: dict[str, Any],
    stable_id: str,
    state_db_path: Path | None = None,
    *,
    has_rb_mapping: bool | None = None,
) -> dict[str, Any]:
    """Apply every own-lane overlay. Mutates and returns ``payload``."""
    payload = apply_own_cues(payload, stable_id, state_db_path)
    return apply_own_waveform(
        apply_own_key_segments(
            apply_own_beatgrid(
                payload, stable_id, state_db_path, has_rb_mapping=has_rb_mapping
            ),
            stable_id,
            state_db_path,
            has_rb_mapping=has_rb_mapping,
        ),
        stable_id,
        state_db_path,
        has_rb_mapping=has_rb_mapping,
    )


__all__ = ["apply_own_overlays"]
