from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from build_xdj_az_panel_geometry import (
    all_px,
    px_to_mm,
    repeat_spread_mm,
    _deck_groups,
    _half,
    _normalise,
)
from build_xdj_az_panel_layout import (
    _ALIGN_COLS,
    _ALIGN_ROWS,
    _CH_CUE_CC,
    _COLOR_CC,
    _FADER_CC,
    _GLOBAL_ALIGN_COLS,
    _GLOBAL_ALIGN_ROWS,
    _HI_CC,
    _LOW_CC,
    _MID_CC,
    _STRIP_ORDER,
    _TRIM_CC,
)
from build_xdj_az_panel_trace import (
    DEVICE,
    IMAGE_PX,
    MM_PER_PX,
    OUT_DIR,
    PANEL_D_MM,
    PANEL_OUT,
    PANEL_PX,
    PANEL_W_MM,
    REFERENCE_OUT,
    RIGHT_DECK_DX_PX,
    SOURCE_IMAGE_SHA256,
    SOURCE_PDF_SHA256,
    _LEFT_DECK_PX,
    _RIGHT_DECK_CHECK_PX,
)
from build_xdj_az_panel_wires_deck import (
    PAD_MODE_OFFSETS,
    PAD_MODE_SELECTORS,
    Control,
    Wire,
    _DECK_ORDER,
    _DECK_ROWS,
    _PAD_FIGS,
)
from build_xdj_az_panel_wires_global import _GLOBAL_ROWS


def _deck_controls(deck: int, pos: dict[str, tuple[float, float, float, float]]) -> list[Control]:
    out: list[Control] = []
    for key in _DECK_ORDER:
        fig, typ, label, does, kind, num, shift_num = _DECK_ROWS[key]
        cid = f"d{deck}-{key}"
        x, y, w, h = pos[cid]
        section = "performance" if key.startswith("pad-mode") else "deck"
        shift = Wire(deck, kind, shift_num) if shift_num is not None else None
        out.append(
            Control(
                cid,
                fig,
                typ,
                label,
                f"{does} (deck {deck})",
                x,
                y,
                w,
                h,
                section,
                Wire(deck, kind, num),
                shift,
            )
        )
    # DECK SELECT: the box carries the channel of the deck it selects.
    select = {1: (3, 1), 2: (2, 4)}[deck]
    for suffix, target in zip(("a", "b"), select, strict=True):
        cid = f"d{deck}-deck-select-{suffix}"
        x, y, w, h = pos[cid]
        out.append(
            Control(
                cid,
                "1-10",
                "button",
                f"DECK {target}",
                f"Select deck {target} on this side",
                x,
                y,
                w,
                h,
                "deck",
                Wire(target, "note", 21),
            )
        )
    for pad in range(8):
        cid = f"d{deck}-pad-{pad + 1}"
        x, y, w, h = pos[cid]
        ch = (5 if pad < 4 else 9) + deck
        note = 19 * (pad % 4)
        out.append(
            Control(
                cid,
                _PAD_FIGS[pad],
                "pad",
                f"PAD {pad + 1}",
                f"Hot cue {'ABCDEFGH'[pad]} (deck {deck}, HOT CUE mode)",
                x,
                y,
                w,
                h,
                "performance",
                Wire(ch, "note", note),
                Wire(ch, "note", note + 1),
            )
        )
    return out


def _strip_controls(ch: int, pos: dict[str, tuple[float, float, float, float]]) -> list[Control]:
    rows: list[tuple[str, str, str, str, str, int]] = [
        ("trim", "3-5", "knob", "TRIM", "Channel gain", _TRIM_CC[ch]),
        ("hi", "3-6", "knob", "HI", "EQ high band", _HI_CC[ch]),
        ("mid", "3-7", "knob", "MID", "EQ mid band", _MID_CC[ch]),
        ("low", "3-8", "knob", "LOW", "EQ low band", _LOW_CC[ch]),
        ("color", "2-14", "knob", "COLOR", "Sound color filter", _COLOR_CC[ch]),
        (
            "cue",
            "3-11",
            "button",
            "CUE",
            "Headphone cue for this channel",
            _CH_CUE_CC[ch],
        ),
        ("fader", "3-2", "fader", "CH FADER", "Channel volume", _FADER_CC[ch]),
    ]
    out: list[Control] = []
    for key, fig, typ, label, does, cc in rows:
        cid = f"ch{ch}-{key}"
        x, y, w, h = pos[cid]
        section = "effect" if key == "color" else "mixer"
        out.append(
            Control(
                cid,
                fig,
                typ,
                label,
                f"{does} (channel {ch})",
                x,
                y,
                w,
                h,
                section,
                Wire(5, "cc", cc),
            )
        )
    return out


def _global_controls(
    pos: dict[str, tuple[float, float, float, float]],
) -> list[Control]:
    out: list[Control] = []
    for cid, (
        fig,
        typ,
        label,
        does,
        section,
        ch,
        kind,
        num,
        shift_num,
    ) in _GLOBAL_ROWS.items():
        x, y, w, h = pos[cid]
        shift = Wire(ch, kind, shift_num) if shift_num is not None else None
        out.append(
            Control(
                cid,
                fig,
                typ,
                label,
                does,
                x,
                y,
                w,
                h,
                section,
                Wire(ch, kind, num),
                shift,
            )
        )
    return out


def _positions() -> tuple[
    dict[str, tuple[float, float, float, float]],
    dict[str, tuple[float, float, float, float]],
]:
    px = all_px()
    raw = {k: px_to_mm(v) for k, v in px.items()}
    rows = _deck_groups(1, _ALIGN_ROWS) + _GLOBAL_ALIGN_ROWS
    cols = _deck_groups(1, _ALIGN_COLS) + _GLOBAL_ALIGN_COLS
    pos = _normalise({k: v for k, v in raw.items() if not k.startswith("d2-")}, rows, cols)
    # The right deck is the normalised left deck translated by one 0.5 mm offset, so the
    # two decks stay an exact translation after rounding.
    dx_mm = _half(RIGHT_DECK_DX_PX * MM_PER_PX["x"])
    for key in _LEFT_DECK_PX:
        x, y, w, h = pos[f"d1-{key}"]
        pos[f"d2-{key}"] = (round(x + dx_mm, 1), y, w, h)
    return raw, pos


def _check(controls: list[Control], pos: dict[str, tuple[float, float, float, float]]) -> None:
    ids = [c.id for c in controls]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise SystemExit(f"[ERROR] duplicate control ids: {dupes}")
    missing = sorted(set(pos) - set(ids))
    extra = sorted(set(ids) - set(pos))
    if missing or extra:
        raise SystemExit(f"[ERROR] trace and controls disagree: untraced={extra} unplaced={missing}")
    for c in controls:
        if c.x < 0 or c.y < 0 or c.x + c.w > PANEL_W_MM or c.y + c.h > PANEL_D_MM:
            raise SystemExit(f"[ERROR] {c.id} lies outside the panel")


def _control_document(control: Control) -> dict[str, object]:
    row = asdict(control)
    if control.type == "pad":
        deck = int(control.id[1])
        pad = int(control.id.rsplit("-", 1)[1]) - 1
        ch = (5 if pad < 4 else 9) + deck
        note = 19 * (pad % 4)
        row["pad_layer_group"] = f"d{deck}"
        row["pad_layers"] = {
            mode: {
                "label": f"{label} PAD {pad + 1}",
                "does": f"Inspect {label} pad {pad + 1} (deck {deck})",
                "midi": asdict(Wire(ch, "note", note + base)) if base is not None else None,
                "shift_midi": asdict(Wire(ch, "note", note + shift)) if shift is not None else None,
            }
            for mode, (label, base, shift) in PAD_MODE_OFFSETS.items()
        }
    elif "-pad-mode-" in control.id:
        base, shift = PAD_MODE_SELECTORS[control.id.split("-pad-mode-", 1)[1]]
        row["pad_layer_group"] = control.id[:2]
        row["pad_layer_select"] = {"base": base, "shift": shift}
    return row


def build() -> tuple[dict[str, object], dict[str, object]]:
    """Return (panel.json document, panel.reference.json document)."""
    raw, pos = _positions()
    controls = [
        *_deck_controls(1, pos),
        *_deck_controls(2, pos),
        *[c for ch in _STRIP_ORDER for c in _strip_controls(ch, pos)],
        *_global_controls(pos),
    ]
    _check(controls, pos)
    px = all_px()
    reference = {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "units": "mm",
        "measured": True,
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "trace": {
            "source": "XDJ-AZ List of MIDI messages E1, page 1, embedded image 0 (top-panel photo, 1217 x 710 px)",
            "source_pdf_sha256": SOURCE_PDF_SHA256,
            "source_image_sha256": SOURCE_IMAGE_SHA256,
            "provenance": "docs/controller/research/XDJ-MIDI-Message-Lists.md",
            "image_px": list(IMAGE_PX),
            "panel_px": dict(PANEL_PX),
            "mm_per_px": dict(MM_PER_PX),
            "method": (
                "Button: body bounding box. Knob and encoder: visible cap. Pad: the pad square. "
                "Jog: outer platter ring. Vertical fader: travel slot with cap width. Crossfader and "
                "X-PAD: travel slot with cap height. Right deck: left-deck boxes translated by "
                f"{RIGHT_DECK_DX_PX} px; {len(_RIGHT_DECK_CHECK_PX)} right-deck controls re-read independently "
                "give repeat_spread_mm. Panel footprint includes the screen housing (depth 504.1 mm)."
            ),
            "repeat_spread_mm": repeat_spread_mm(),
            "issue": "#9366",
            "date": "Sun 8 Nov 2026",
        },
        "controls": [
            {
                "id": c.id,
                "px": list(px[c.id]),
                "x": raw[c.id][0],
                "y": raw[c.id][1],
                "w": raw[c.id][2],
                "h": raw[c.id][3],
            }
            for c in controls
        ],
    }
    panel = {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "display_name": "4-channel all-in-one system",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": "XDJ_AZ_MAP",
        "sources": {
            "panel_size": "XDJ-AZ Instruction Manual, Specifications: max. external dimensions (W x D x H) 895 x 504.1 x 133.4 mm",
            "wire": "apps/webui/frontend/src/lib/rb/midi/maps/pioneer-xdj.ts (XDJ-AZ List of MIDI messages E1)",
            "positions": (
                "measured trace of the official top-panel photo, normalised for the renderer; "
                "reference: devices/xdj-az/panel.reference.json (ADR-0137)"
            ),
        },
        "out_of_scope": [
            "deck 3 and deck 4 transport: the left deck carries deck 1 wires and the right deck deck 2 wires "
            "(power-on selection); DECK SELECT boxes carry the selected deck's channel",
            "MSB/LSB companion CCs: TEMPO carries its MSB",
            "hardware-only controls with no MIDI row: JOG FEEL, JOG RING, MIC 1/2 LEVEL, SHORT CUT, MIXER SETTINGS, the six screen buttons, USB STOP",
            "FX SELECT is a rotary switch with one CC per position; the control carries the DELAY position",
            "touch screen, needle search on the overall waveform, level meters and the jog displays",
            "crossfader assign and input select switches (utility settings, not panel controls)",
        ],
        "pad_layer_groups": {
            f"d{deck}": {
                "label": f"{'Left' if deck == 1 else 'Right'} pad layer",
                "default_mode": "hot_cue",
                "modes": [{"id": mode, "label": row[0]} for mode, row in PAD_MODE_OFFSETS.items()],
            }
            for deck in (1, 2)
        },
        "controls": [_control_document(c) for c in controls],
    }
    return panel, reference
