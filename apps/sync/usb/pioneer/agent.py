"""Claude Sonnet 4.6 computer-use agent loop for Rekordbox USB export.

This module drives a live Rekordbox 7 GUI through the Anthropic
computer-use beta. It is the "skip the format, let Rekordbox do it" path
from ``docs/rb-usb-export-automation.md``: instead of synthesising the
Pioneer USB format ourselves, we delegate the export clicks to a vision-
capable model.

Architecture
------------

::

    ┌─────────────────┐   tool_use   ┌────────────────────────┐
    │ Claude Sonnet   │─────────────▶│  Actuator              │
    │   (computer     │              │  (cliclick + Quartz)   │
    │    use beta)    │◀─ result ────│                        │
    └─────────────────┘              └────────────────────────┘
           ▲                                   │
           │                                   ▼
           │                             ┌──────────┐
           │                             │ Trace dir│
           │                             └──────────┘
           │
           │ prompt = task spec
           │

Every loop iteration:

1. Send conversation to Claude with the ``computer`` tool available.
2. If Claude returns a ``tool_use`` for the computer tool, dispatch it
   via :class:`Actuator` and feed the result (new screenshot + meta) back
   as a ``tool_result``.
3. If Claude returns a plain ``end_turn`` with no tool use, we stop.
4. Hard cap at ``max_steps`` iterations to prevent runaway loops.

Model + beta selection
----------------------

The Anthropic model list reported these Sonnet candidates at the time of
writing::

    claude-sonnet-4-6         ← current Sonnet (preferred)
    claude-sonnet-4-5-20250929
    claude-sonnet-4-20250514  ← fallback if 4-6 not available

The computer-use beta header is ``computer-use-2025-01-24``. If Anthropic
rolls a newer revision, override via ``beta_header=``.

Requirement: CAT-06.
"""
from __future__ import annotations

import base64
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent_actuator import Actuator, Trace, ensure_frontmost

LOGGER = logging.getLogger("rb.agent")


# Sonnet candidates in preference order. First one that doesn't 404 wins.
DEFAULT_MODEL_CANDIDATES = (
    "claude-sonnet-4-6",
    "claude-sonnet-4-5-20250929",
    "claude-sonnet-4-20250514",
)

# Computer-use tool version. Must match `computer_YYYYMMDD` in the tool
# spec. Updated to 2025-01-24 per Anthropic's current docs.
COMPUTER_TOOL_TYPE = "computer_20250124"
COMPUTER_BETA_HEADER = "computer-use-2025-01-24"

MAX_STEPS_DEFAULT = 30

# Pricing for Sonnet 4.x computer-use calls. Used only for the end-of-run
# cost estimate; not authoritative — confirm with Anthropic's billing page.
# $ / M tokens as of Apr 2026.
PRICE_IN_PER_M = 3.0
PRICE_OUT_PER_M = 15.0


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


@dataclass
class AgentConfig:
    playlist: str
    usb_path: str
    dry_run: bool = True
    max_steps: int = MAX_STEPS_DEFAULT
    # Bumped 1280→1920: Sonnet 4.5 accepts this reliably and it keeps
    # small widgets (Sync Manager button) visible after downsampling.
    # Cost delta at 30 screenshots: ~+45k input tokens = +$0.14.
    downsample_width: int = 1920
    trace_dir: Path = Path("apps/sync/usb/pioneer/traces")
    model_candidates: tuple[str, ...] = DEFAULT_MODEL_CANDIDATES
    beta_header: str = COMPUTER_BETA_HEADER
    api_key: str | None = None
    # When set, screenshots are isolated to this app's window (Fix 1).
    # Set to None to always full-display-capture (legacy).
    window_app: str | None = "rekordbox"
    # App to re-activate before clicks (Fix 3). None disables.
    frontmost_app: str | None = "rekordbox"


@dataclass
class AgentResult:
    success: bool
    model: str
    steps: int
    stop_reason: str
    trace_root: Path
    api_calls: list[dict[str, Any]] = field(default_factory=list)
    usage_in_tokens: int = 0
    usage_out_tokens: int = 0
    final_text: str = ""
    action_summaries: list[str] = field(default_factory=list)

    @property
    def estimated_cost_usd(self) -> float:
        return (
            self.usage_in_tokens / 1_000_000 * PRICE_IN_PER_M
            + self.usage_out_tokens / 1_000_000 * PRICE_OUT_PER_M
        )


# --------------------------------------------------------------------------- #
# System prompt construction
# --------------------------------------------------------------------------- #


def build_system_prompt(cfg: AgentConfig) -> str:
    dry_note = (
        "\n\n**DRY-RUN MODE IS ACTIVE.** Your clicks are REAL — dismiss any "
        "preliminary dialogs, open Sync Manager, and select the playlist "
        "normally. The ONLY click you must not do is the final "
        "Export/Sync/Start button that begins the USB write. When you have "
        "the playlist selected and are looking at the export button, STOP: "
        "describe what the Export button looks like (colour, label, "
        "coordinates) and end your turn with the literal text "
        "`END_TURN: DRY-RUN COMPLETE`.\n"
    ) if cfg.dry_run else (
        "\n\n**LIVE MODE.** You may click the Export/Sync button once you "
        "have verified you have the correct playlist selected and the USB "
        "target is visible. After the click, take screenshots to confirm "
        "the progress dialog / completion.\n"
    )
    return f"""You are an expert macOS UI-automation agent. Your job is to drive \
Rekordbox 7 to export a playlist to a USB stick. Think step-by-step, but \
keep your narration concise.

TASK
----
Export the playlist named "{cfg.playlist}" to the USB volume mounted at \
"{cfg.usb_path}".

SCREENSHOT SCOPE — READ THIS FIRST
----------------------------------
The screenshots you receive are **cropped to the Rekordbox window only**
(not the full desktop). Coordinates (0,0) are the TOP-LEFT of the
Rekordbox window. The runtime translates your click back to the
correct screen position — you don't need to compensate for window
placement yourself.

If a screenshot looks unusually small or like a plain desktop (no decks,
no waveforms), that means Rekordbox is minimised / not visible and the
runtime fell back to full-display capture. In that case your first
action should be a screenshot after a short `wait` — the runtime will
re-activate Rekordbox for you.

REKORDBOX 7.2.14 WINDOW LAYOUT
------------------------------
* **Top area (roughly top 45% of window height):** decks with
  waveforms, jog wheels, track info. IGNORE this area for export —
  nothing you need is up there.
* **Left sidebar (column of the lower half, ~15% of width):**
  the "Media Browser" tree with nodes:
    - COLLECTION            (your whole library)
    - RELATED TRACKS / HISTORY / TAG LIST (metadata views)
    - PLAYLISTS             ← "{cfg.playlist}" lives under here
    - DEVICES / USB DRIVES  ← "MAINTAINER" appears here when mounted
* **Bottom-left toolbar:** a row of small icons just above the track
  list or at the very bottom of the window. Icons include
  search, filter, create-playlist, and (the important one)
  the **Sync Manager** button — a small icon with **two opposing
  arrows** (``⇄`` / ``↔``). Hovering it shows the tooltip
  "Sync Manager". It is usually at X ≈ 15–60 of the window-local
  frame, near the bottom edge (Y ≈ 90% of window height).
* **Right of the sidebar:** the track list — columns like #, Title,
  Artist, BPM, Key, Rating.

CANONICAL EXPORT FLOW
---------------------
1. Click the **Sync Manager button** (two-arrow icon, bottom-left).
2. A panel/window opens with a library tree on its LEFT and device
   contents on its RIGHT.
3. Find "{cfg.playlist}" in the left tree and TICK ITS CHECKBOX.
4. Click the right-pointing Export / Sync arrow button at the TOP
   of the Sync Manager panel.
5. Handle any confirmation dialog (usually "Start export?" → OK).
6. Wait for the progress bar / "Export complete" to appear.

ALTERNATIVE (use only if Sync Manager unreachable)
--------------------------------------------------
* Find "{cfg.playlist}" under Playlists in the sidebar.
* Drag it directly onto the "MAINTAINER" node under Devices.
* Confirm any dialog that appears.

OPERATING RULES
---------------
1. ALWAYS take a screenshot first to see current state. Do not act blind.
2. After each click, use `wait` ~0.5s and take another screenshot to
   verify the UI responded.
3. **Only click inside the Rekordbox window.** The runtime rejects
   clicks that fall outside — if you get an error saying "click
   outside window", you have mis-identified where the target is.
   Take a fresh screenshot and reassess.
4. If you can't find the Sync Manager button after 3 screenshots,
   switch to the drag alternative.
5. Prefer small, targeted clicks (≤ 40px element). Avoid dragging
   unless intentional.
6. If you see a licensing / sign-in / demo / subscription dialog,
   STOP and reply with `EXPORT_FAILED: <describe dialog>`.
7. If you see a modal dialog you don't recognise (conversion prompt,
   USB format warning, OneLibrary prompt), STOP, describe it, and
   include "UNEXPECTED_DIALOG" in your response — do not dismiss it.
8. Do NOT quit Rekordbox, close its windows, or sign out.
9. If you cannot find the playlist after 3 attempts, report what you
   saw and stop. Do not guess wildly.
{dry_note}
SUCCESS CRITERIA
----------------
Stop and say "END_TURN: COMPLETE" when one of:
* (dry-run) you have "{cfg.playlist}" ticked in Sync Manager and are
  looking at the Export button. End with "END_TURN: DRY-RUN COMPLETE".
* (live) a progress indicator or "Export complete" message is visible.
"""


# --------------------------------------------------------------------------- #
# Message / content helpers
# --------------------------------------------------------------------------- #


def _image_block(png_bytes: bytes) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.standard_b64encode(png_bytes).decode("ascii"),
        },
    }


def _summarise_action(action: dict[str, Any], result: dict[str, Any]) -> str:
    a = action.get("action") or action.get("type") or "?"
    outcome = result.get("meta", {}).get("outcome", "?")
    extra = ""
    if "coordinate" in action:
        extra = f" @ {action['coordinate']}"
    elif action.get("text"):
        t = action["text"]
        extra = f" {t!r}" if len(t) < 40 else f" <{len(t)}-char text>"
    return f"{a}{extra} → {outcome}"


# --------------------------------------------------------------------------- #
# Model resolution
# --------------------------------------------------------------------------- #


def resolve_model(
    client: Any,
    candidates: tuple[str, ...],
    *,
    beta_header: str,
    geometry_px: tuple[int, int],
) -> tuple[str, list[dict[str, Any]]]:
    """Try each candidate model with a trivial no-op message. First to
    respond 200 wins. Returns ``(model_name, probes_meta)``.
    """
    probes: list[dict[str, Any]] = []
    w, h = geometry_px
    for model in candidates:
        try:
            resp = client.beta.messages.create(
                model=model,
                max_tokens=32,
                betas=[beta_header],
                tools=[
                    {
                        "type": COMPUTER_TOOL_TYPE,
                        "name": "computer",
                        "display_width_px": w,
                        "display_height_px": h,
                    }
                ],
                messages=[{"role": "user", "content": "probe"}],
            )
            probes.append(
                {
                    "model": model,
                    "status": "ok",
                    "id": resp.id,
                    "stop_reason": resp.stop_reason,
                }
            )
            return model, probes
        except Exception as exc:  # pragma: no cover - network
            probes.append(
                {
                    "model": model,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            LOGGER.warning("model %s failed probe: %s", model, exc)
    raise RuntimeError(f"No candidate model succeeded. Probes: {probes}")


# --------------------------------------------------------------------------- #
# Main agent loop
# --------------------------------------------------------------------------- #


def run_export_agent(
    cfg: AgentConfig,
    *,
    client: Any | None = None,
    on_step: Any = None,
) -> AgentResult:
    """Run the Claude export agent end-to-end.

    ``client`` may be provided by tests to inject a mock Anthropic SDK.
    ``on_step`` is an optional callback ``(i, message, actions)`` invoked
    after each API round-trip.
    """
    api_key = cfg.api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY missing. Export via doppler or env var."
        )

    if client is None:  # pragma: no cover - requires network
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)

    trace = Trace.new(Path(cfg.trace_dir))
    # NOTE: we do NOT pass `simulated=cfg.dry_run`. Dry-run is a system-
    # prompt discipline ("don't click Export"); the actuator must still
    # click through every preliminary step (dismiss modals, open Sync
    # Manager, select playlist) or the agent can never reach the export
    # surface to "dry-run stop" against. See docstring on ``Actuator``.
    actuator = Actuator(
        trace,
        downsample_width=cfg.downsample_width,
        dry_run=cfg.dry_run,
        window_app=cfg.window_app,
        frontmost_app=cfg.frontmost_app,
    )
    # Pre-flight: bring Rekordbox to the front BEFORE the first
    # capture so the window-isolated path finds it on-screen (Fix 3).
    # Non-fatal if it can't — capture_window falls back to display.
    # allow_launch=True is deliberate and is the ONLY caller that may
    # start Rekordbox: a real export run needs the app up, and the user
    # asked for it by invoking agent-export. Every other caller (the
    # per-click re-focus in Actuator._dispatch) keeps the safe default,
    # so a stray focus check can never cold-start the app.
    if cfg.frontmost_app:
        try:
            ok = ensure_frontmost(
                cfg.frontmost_app, max_attempts=3, allow_launch=True
            )
            trace.record(
                "preflight_frontmost",
                {"app": cfg.frontmost_app, "ok": bool(ok)},
            )
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("frontmost preflight failed: %s", exc)

    # Use the SCALED screenshot size as the logical display for Claude —
    # that is what it actually "sees". The actuator handles conversion
    # back to screen points internally.
    capture = actuator.capture()
    scaled_w, scaled_h = capture["scaled_size"]
    geometry_px = (scaled_w, scaled_h)

    trace.record(
        "run_start",
        {
            "playlist": cfg.playlist,
            "usb_path": cfg.usb_path,
            "dry_run": cfg.dry_run,
            "max_steps": cfg.max_steps,
            "display_px": list(geometry_px),
            "model_candidates": list(cfg.model_candidates),
        },
    )

    model, probes = resolve_model(
        client,
        cfg.model_candidates,
        beta_header=cfg.beta_header,
        geometry_px=geometry_px,
    )
    LOGGER.info("selected model: %s", model)
    trace.record("model_selected", {"model": model, "probes": probes})

    # Initial user message with the first screenshot already embedded.
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Please export the Rekordbox playlist "
                        f"{cfg.playlist!r} to the USB at "
                        f"{cfg.usb_path!r}. Current desktop screenshot "
                        f"is attached — start by taking your own "
                        f"screenshot to confirm the state, then proceed."
                    ),
                },
                _image_block(capture["png_bytes"]),
            ],
        }
    ]

    computer_tool = {
        "type": COMPUTER_TOOL_TYPE,
        "name": "computer",
        "display_width_px": scaled_w,
        "display_height_px": scaled_h,
    }

    api_calls: list[dict[str, Any]] = [
        {"phase": "probe", "probes": probes}
    ]
    total_in = 0
    total_out = 0
    action_summaries: list[str] = []
    stop_reason = "unknown"
    final_text = ""

    system_prompt = build_system_prompt(cfg)

    for step in range(1, cfg.max_steps + 1):
        LOGGER.info("=== agent step %d ===", step)
        t0 = time.time()
        try:
            resp = client.beta.messages.create(
                model=model,
                max_tokens=1024,
                betas=[cfg.beta_header],
                tools=[computer_tool],
                system=system_prompt,
                messages=messages,
            )
        except Exception as exc:  # pragma: no cover
            trace.record(
                "api_error",
                {"step": step, "error": f"{type(exc).__name__}: {exc}"},
            )
            api_calls.append(
                {
                    "step": step,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            stop_reason = "api_error"
            break

        usage = getattr(resp, "usage", None)
        if usage is not None:
            total_in += getattr(usage, "input_tokens", 0) or 0
            total_out += getattr(usage, "output_tokens", 0) or 0
        api_calls.append(
            {
                "step": step,
                "status": "ok",
                "id": resp.id,
                "stop_reason": resp.stop_reason,
                "latency_ms": int((time.time() - t0) * 1000),
                "usage": {
                    "in": getattr(usage, "input_tokens", 0) if usage else 0,
                    "out": getattr(usage, "output_tokens", 0) if usage else 0,
                },
            }
        )

        # Collect text + tool_use blocks.
        assistant_blocks: list[Any] = []
        tool_uses: list[Any] = []
        step_text_parts: list[str] = []
        for block in resp.content:
            assistant_blocks.append(block.model_dump() if hasattr(block, "model_dump") else block)
            btype = getattr(block, "type", None)
            if btype == "text":
                step_text_parts.append(block.text)
            elif btype == "tool_use":
                tool_uses.append(block)

        step_text = "\n".join(step_text_parts).strip()
        trace.record(
            "assistant",
            {
                "step": step,
                "stop_reason": resp.stop_reason,
                "text": step_text,
                "tool_use_count": len(tool_uses),
                "usage": {
                    "in": getattr(usage, "input_tokens", 0) if usage else 0,
                    "out": getattr(usage, "output_tokens", 0) if usage else 0,
                },
            },
        )

        # Append the assistant turn to the conversation.
        messages.append({"role": "assistant", "content": assistant_blocks})

        if resp.stop_reason != "tool_use" or not tool_uses:
            # Agent is done talking.
            final_text = step_text
            stop_reason = resp.stop_reason or "end_turn"
            if on_step is not None:
                on_step(step, resp, [])
            break

        # Dispatch each tool_use in order and build the user response.
        tool_results_content: list[dict[str, Any]] = []
        step_actions: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for tu in tool_uses:
            action_input = tu.input or {}
            summary_input = dict(action_input)  # copy for summary
            exec_result = actuator.execute_action(action_input)
            step_actions.append((action_input, exec_result))
            action_summaries.append(_summarise_action(summary_input, exec_result))

            content_blocks: list[dict[str, Any]] = []
            is_error = not exec_result.get("ok", True)
            outcome = exec_result.get("meta", {}).get("outcome", "?")

            # Anthropic rejects tool_result with image content when
            # `is_error=true` — errors MUST be text-only. Skip the
            # screenshot in that case and surface the failure text.
            if is_error:
                err_msg = exec_result.get("error") or outcome
                content_blocks.append(
                    {
                        "type": "text",
                        "text": f"ERROR: {err_msg}",
                    }
                )
            else:
                if exec_result.get("screenshot"):
                    # Claude expects a screenshot back for `screenshot` actions.
                    content_blocks.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": exec_result["screenshot"],
                            },
                        }
                    )
                else:
                    # After non-screenshot actions, also send a fresh
                    # screenshot so the model can verify the UI state
                    # changed.
                    cap = actuator.capture()
                    content_blocks.append(_image_block(cap["png_bytes"]))
                content_blocks.append(
                    {"type": "text", "text": f"outcome={outcome}"}
                )

            tool_results_content.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": content_blocks,
                    "is_error": is_error,
                }
            )

        messages.append({"role": "user", "content": tool_results_content})

        if on_step is not None:
            on_step(step, resp, step_actions)
    else:
        stop_reason = "max_steps"

    trace.record(
        "run_end",
        {
            "stop_reason": stop_reason,
            "steps": step,
            "final_text": final_text,
            "actions": action_summaries,
            "usage": {"in": total_in, "out": total_out},
            "cost_usd": round(
                total_in / 1_000_000 * PRICE_IN_PER_M
                + total_out / 1_000_000 * PRICE_OUT_PER_M,
                4,
            ),
        },
    )

    return AgentResult(
        success=stop_reason in {"end_turn", "stop_sequence"},
        model=model,
        steps=step,
        stop_reason=stop_reason,
        trace_root=trace.root,
        api_calls=api_calls,
        usage_in_tokens=total_in,
        usage_out_tokens=total_out,
        final_text=final_text,
        action_summaries=action_summaries,
    )


__all__ = [
    "AgentConfig",
    "AgentResult",
    "run_export_agent",
    "build_system_prompt",
    "resolve_model",
    "DEFAULT_MODEL_CANDIDATES",
    "COMPUTER_TOOL_TYPE",
    "COMPUTER_BETA_HEADER",
]
