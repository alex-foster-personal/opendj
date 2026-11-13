"""Change what the user sees: waveform design, waveform overlays, waveform zoom, layout preset and skin.

Each tool is one existing bus verb (``set_waveform_design``,
``set_waveform_overlay``, ``set_wave_zoom``, ``set_layout_preset``,
``set_skin`` in ``apps/opendj_cli/catalog.py``), built into an AGENT-03 single
order by the CLI's own parser and posted to ``POST /api/v1/commands``. So
``opendj set_waveform_design rgb``, MCP ``command`` and OJ
``view_waveform_design`` send the identical order. Enums come from the catalog
and are checked here before any request, together with the pairings the page
refuses (design or height, grid_lines takes a value and the switches take
enabled, a preset of the named surface). They only change the view, but they
act on the app, so each is ``confirm`` gated, as the generic ``command`` tool
is. The answer is judged whole by ``core_tools.require_steps_succeeded``, then
cut to the order and its steps.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure, ToolRun, ToolSpec
from apps.engine_core.assistant.core_tools import COMMANDS_PATH, engine_json, require_steps_succeeded
from apps.engine_core.assistant.oj_tools._args import int_arg
from apps.opendj_cli.catalog import (
    _LIBRARY_LAYOUT_VALUES,
    _PERFORMANCE_LAYOUT_VALUES,
    LAYOUT_PRESET_VALUES,
    LAYOUT_SURFACE_VALUES,
    UI_SKIN_VALUES,
    VERBS,
    WAVE_GRID_LINES_VALUES,
    WAVE_HEIGHT_VALUES,
    WAVE_PALETTE_VALUES,
    WAVE_SPLIT_MASTER_VALUES,
    WAVE_ZOOM_VALUES,
    WAVEFORM_DESIGN_VALUES,
    WAVEFORM_OVERLAY_VALUES,
)
from apps.opendj_cli.kinds import DECK_VALUES
from apps.opendj_cli.orders import single
from apps.opendj_cli.verbs import InvocationError, parse_invocation

# ----- configuration ------------------------------------------------------
LOW_DECK: int = min(DECK_VALUES)
HIGH_DECK: int = max(DECK_VALUES)
GRID_LINES: str = "grid_lines"
#: set_skin sets all three at once; a call naming only the skin lets the skin pick its palette and split.
SKIN_PART_DEFAULT: str = "auto"
SURFACE_PRESETS: dict[str, tuple[str, ...]] = {
    "performance": ("auto", *_PERFORMANCE_LAYOUT_VALUES),
    "library": ("auto", *_LIBRARY_LAYOUT_VALUES),
}


# ----- argument checks ----------------------------------------------------
def _choice(args: dict[str, Any], key: str, choices: Sequence[str]) -> str:
    """``args[key]`` when it is one of ``choices``, else ``invalid_arguments`` naming them."""
    value = args.get(key)
    if value not in choices:
        raise ToolFailure("invalid_arguments", f"{key} must be one of {'|'.join(choices)}, got {value!r}")
    return str(value)


def _optional_choice(args: dict[str, Any], key: str, choices: Sequence[str]) -> str | None:
    return None if args.get(key) is None else _choice(args, key, choices)


def _enabled(args: dict[str, Any]) -> bool:
    enabled = args.get("enabled")
    if not isinstance(enabled, bool):
        raise ToolFailure("invalid_arguments", f"enabled is required: true or false, got {enabled!r}")
    return enabled


def _zoom(args: dict[str, Any]) -> str:
    """The zoom as the catalog spells it; a whole number of bars (8) is accepted as its text ("8")."""
    zoom = args.get("zoom")
    if isinstance(zoom, int) and not isinstance(zoom, bool):
        zoom = str(zoom)
    return _choice({"zoom": zoom}, "zoom", WAVE_ZOOM_VALUES)


# ----- order helpers ------------------------------------------------------
def _view_order(tokens: list[str]) -> dict[str, Any]:
    """The AGENT-03 single order the CLI parser builds for ``tokens`` (verb first, then key=value arguments)."""
    try:
        return single(parse_invocation(tokens))
    except (InvocationError, ValueError) as exc:
        raise ToolFailure("invalid_arguments", str(exc)) from exc


async def _post(ctx: ToolContext, tokens: list[str]) -> dict[str, Any]:
    order = _view_order(tokens)
    result = require_steps_succeeded(await engine_json(ctx, "POST", COMMANDS_PATH, order))
    return {"order": order, "steps": result["steps"]}


def _named(**parts: str | int | None) -> list[str]:
    return [f"{key}={value}" for key, value in parts.items() if value is not None]


# ----- tool runs ----------------------------------------------------------
async def _waveform_design(ctx: ToolContext, args: dict[str, Any]) -> Any:
    design = _optional_choice(args, "design", WAVEFORM_DESIGN_VALUES)
    height = _optional_choice(args, "height", WAVE_HEIGHT_VALUES)
    if design is None and height is None:
        raise ToolFailure("invalid_arguments", "name design, height or both")
    return await _post(ctx, ["set_waveform_design", *_named(design=design, height=height)])


async def _waveform_overlay(ctx: ToolContext, args: dict[str, Any]) -> Any:
    overlay = _choice(args, "overlay", WAVEFORM_OVERLAY_VALUES)
    if overlay == GRID_LINES:
        if args.get("enabled") is not None:
            raise ToolFailure("invalid_arguments", "grid_lines takes value, not enabled")
        value = _choice(args, "value", WAVE_GRID_LINES_VALUES)
        return await _post(ctx, ["set_waveform_overlay", *_named(overlay=overlay, value=value)])
    if args.get("value") is not None:
        raise ToolFailure("invalid_arguments", f"{overlay} takes enabled; value is for grid_lines only")
    enabled = str(_enabled(args)).lower()
    return await _post(ctx, ["set_waveform_overlay", *_named(overlay=overlay, enabled=enabled)])


async def _wave_zoom(ctx: ToolContext, args: dict[str, Any]) -> Any:
    zoom = _zoom(args)
    deck = int_arg(args, "deck", low=LOW_DECK, high=HIGH_DECK)
    return await _post(ctx, ["set_wave_zoom", *_named(zoom=zoom, deck=deck)])


async def _layout_preset(ctx: ToolContext, args: dict[str, Any]) -> Any:
    surface = _choice(args, "surface", LAYOUT_SURFACE_VALUES)
    preset = _choice(args, "preset", SURFACE_PRESETS[surface])
    return await _post(ctx, ["set_layout_preset", *_named(surface=surface, preset=preset)])


async def _skin(ctx: ToolContext, args: dict[str, Any]) -> Any:
    skin = _choice(args, "skin", UI_SKIN_VALUES)
    palette = _optional_choice(args, "wave_palette", WAVE_PALETTE_VALUES) or SKIN_PART_DEFAULT
    split = _optional_choice(args, "wave_split_master", WAVE_SPLIT_MASTER_VALUES) or SKIN_PART_DEFAULT
    return await _post(ctx, ["set_skin", *_named(ui_skin=skin, wave_palette=palette, wave_split_master=split)])


# ----- schemas ------------------------------------------------------------
def _enum(choices: Sequence[str], description: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(choices), "description": description}


_FAILS = "Fails no_performance_page when no page is open, command_failed when the page could not do it."


def _spec(
    name: str,
    verb: str,
    does: str,
    properties: dict[str, Any],
    required: list[str],
    run: ToolRun,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=(
            f"{does} {_FAILS} The user approves each call. CLI: opendj {verb} {VERBS[verb].usage()}; MCP: command."
        ),
        parameters={"type": "object", "properties": properties, "required": required},
        gate="confirm",
        run=run,
    )


TOOLS: tuple[ToolSpec, ...] = (
    _spec(
        "view_layout_preset",
        "set_layout_preset",
        "Pick the screen layout for one surface; auto follows the skin's own layout.",
        {
            "surface": _enum(LAYOUT_SURFACE_VALUES, "Which screen the preset is for."),
            "preset": _enum(
                LAYOUT_PRESET_VALUES,
                f"performance takes auto|{'|'.join(_PERFORMANCE_LAYOUT_VALUES)}; "
                f"library takes auto|{'|'.join(_LIBRARY_LAYOUT_VALUES)}.",
            ),
        },
        ["surface", "preset"],
        _layout_preset,
    ),
    _spec(
        "view_skin",
        "set_skin",
        "Switch the app skin (for example light), with its waveform palette and split master waveform.",
        {
            "skin": _enum(UI_SKIN_VALUES, "The chrome skin."),
            "wave_palette": _enum(WAVE_PALETTE_VALUES, "Waveform colours; omitted means auto (the skin's own)."),
            "wave_split_master": _enum(
                WAVE_SPLIT_MASTER_VALUES, "Split master waveform; omitted means auto (off on every skin)."
            ),
        },
        ["skin"],
        _skin,
    ),
    _spec(
        "view_wave_zoom",
        "set_wave_zoom",
        "Set the main waveform zoom in bars across the row; zoom out is a larger number, whole shows the track.",
        {
            "zoom": _enum(WAVE_ZOOM_VALUES, "Bars across the row, or whole."),
            "deck": {
                "type": "integer",
                "minimum": LOW_DECK,
                "maximum": HIGH_DECK,
                "description": f"Deck {LOW_DECK} to {HIGH_DECK}; only matters while the zoom is unlinked.",
            },
        },
        ["zoom"],
        _wave_zoom,
    ),
    _spec(
        "view_waveform_design",
        "set_waveform_design",
        "Change how waveforms are drawn (for example rgb) and their height; name design, height or both.",
        {
            "design": _enum(WAVEFORM_DESIGN_VALUES, "Waveform drawing style."),
            "height": _enum(WAVE_HEIGHT_VALUES, "full mirrors the waveform; half draws it from the bottom (CDJ)."),
        },
        [],
        _waveform_design,
    ),
    _spec(
        "view_waveform_overlay",
        "set_waveform_overlay",
        "Show or hide one waveform overlay (enabled), or set grid_lines (value).",
        {
            "overlay": _enum(WAVEFORM_OVERLAY_VALUES, "Which overlay."),
            "enabled": {"type": "boolean", "description": "Show (true) or hide (false); every overlay but grid_lines."},
            "value": _enum(WAVE_GRID_LINES_VALUES, "grid_lines only: beat and bar lines, bar lines only, or off."),
        },
        ["overlay"],
        _waveform_overlay,
    ),
)
