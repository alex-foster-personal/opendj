"""Public, metadata-only presentation for a shared set history."""
# ruff: noqa: E501
from __future__ import annotations

import json
from html import escape
from math import isfinite

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from . import paths as sets_paths
from .sessions import Session, get_session

router = APIRouter()


def _elapsed(value: object) -> str:
    if not isinstance(value, int | float) or not isfinite(value) or value < 0:
        raise ValueError("shared history timestamp must be non-negative and finite")
    seconds = int(value)
    return f"{seconds // 60}:{seconds % 60:02d}"


def _timeline_rows(session_id: str) -> list[tuple[str, str, str]]:
    path = sets_paths.session_dir(session_id) / "timeline.jsonl"
    if not path.exists():
        return []
    rows: list[tuple[str, str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if not isinstance(event, dict) or event.get("action") != "track_loaded":
            continue
        metadata = event.get("value")
        values = metadata if isinstance(metadata, dict) else {}
        title = values.get("title") or event.get("track_stable_id") or "Unidentified track"
        artist = values.get("artist") or event.get("source") or "Recorded source"
        deck = event.get("deck")
        detail = f"{artist} - Deck {deck}" if deck else str(artist)
        rows.append((_elapsed(event.get("timestamp_s")), str(title), detail))
    return rows


def _transition_rows(session: Session) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for transition in session.transitions:
        from_track = transition.get("from_track") or transition.get("from_deck") or "No recorded track"
        to_track = transition.get("to_track") or transition.get("to_deck") or "No recorded track"
        confidence = transition.get("confidence")
        if not isinstance(confidence, int | float) or not isfinite(confidence):
            raise ValueError("shared transition confidence must be finite")
        rows.append((_elapsed(transition.get("t_change_s")), f"{from_track} to {to_track}", f"{transition.get('predicted_class', 'transition')} - {confidence:.0%} confidence"))
    return rows


def _list(rows: list[tuple[str, str, str]], empty: str) -> str:
    if not rows:
        return f"<p class=\"empty\">{escape(empty)}</p>"
    return "".join(f"<li><time>{escape(elapsed)}</time><div><strong>{escape(title)}</strong><small>{escape(detail)}</small></div></li>" for elapsed, title, detail in rows)


def _render(session: Session) -> str:
    tracks = _timeline_rows(session.summary.session_id)
    transitions = _transition_rows(session)
    duration = "Duration not finalized" if session.summary.duration_s is None else _elapsed(session.summary.duration_s)
    started_at = escape(session.summary.started_at.replace("T", " ").replace("+00:00", " UTC"))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Shared set - Open DJ</title><style>
:root{{color-scheme:dark;font-family:Inter,ui-sans-serif,system-ui,sans-serif;background:#081014;color:#e8f0f4}}body{{margin:0}}main{{width:min(920px,calc(100% - 2rem));margin:0 auto;padding:clamp(1.5rem,5vw,4.5rem) 0 2rem}}h1,h2,p{{margin:0}}h1{{font-size:clamp(2.4rem,7vw,5rem);letter-spacing:-.05em}}.eyebrow{{color:#6fc4de;font-size:.72rem;font-weight:800;letter-spacing:.18em}}.lede,.empty,footer{{color:#a9bdc5;margin-top:.5rem}}.hero{{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;overflow:hidden;margin-top:2rem;background:#2a4650;border:1px solid #2a4650;border-radius:12px}}.hero div{{min-height:90px;padding:1rem;background:#102027}}.hero span,small{{display:block;color:#98adb5;font-size:.74rem}}.hero strong{{display:block;margin-top:.45rem}}section{{margin-top:1.5rem;background:#0d1a20;border:1px solid #29434e;border-radius:12px;padding:1.2rem}}.heading{{display:flex;justify-content:space-between;gap:1rem;align-items:end;margin-bottom:.85rem}}.heading h2{{margin-top:.25rem;font-size:1.4rem}}ol{{list-style:none;margin:0;padding:0}}li{{display:grid;grid-template-columns:58px minmax(0,1fr);gap:.85rem;padding:.8rem 0;border-top:1px solid #213740}}time{{color:#b4dbe6;font:.8rem ui-monospace,monospace;text-align:right}}small{{margin-top:.25rem}}footer{{font-size:.75rem;text-align:center}}@media(max-width:680px){{.hero{{grid-template-columns:repeat(2,1fr)}}.heading{{align-items:start;flex-direction:column}}}}</style></head><body><main><header><p class="eyebrow">OPEN DJ SET HISTORY</p><h1>Shared set</h1><p class="lede">A real performance timeline, shared as metadata only.</p></header><section class="hero" aria-label="Set overview"><div><span>Started</span><strong>{started_at}</strong></div><div><span>Duration</span><strong>{escape(duration)}</strong></div><div><span>Played</span><strong>{len(tracks)} tracks</strong></div><div><span>Transitions</span><strong>{len(transitions)} detected</strong></div></section><section aria-label="Played track order"><div class="heading"><div><p class="eyebrow">PERFORMANCE</p><h2>Track order</h2></div><span>{len(tracks)} real timeline events</span></div><ol>{_list(tracks, "This finalized set has no recorded track events.")}</ol></section><section aria-label="Detected transitions"><div class="heading"><div><p class="eyebrow">FLOW</p><h2>Transitions</h2></div><span>{len(transitions)} from the recorded timeline</span></div><ol>{_list(transitions, "No transitions were recorded for this set.")}</ol></section><footer>Metadata-only share. Open DJ does not publish or stream recorded audio from this view.</footer></main></body></html>"""


@router.get("/sets/shared/{session_id}", include_in_schema=False)
def shared_set_page(session_id: str) -> HTMLResponse:
    """Return one published set only, with no audio element or segment metadata."""
    try:
        session = get_session(session_id)
    except sets_paths.SessionPathError as exc:
        raise HTTPException(status_code=404, detail="shared set not found") from exc
    if session is None or session.summary.share_state != "shared_cloud":
        raise HTTPException(status_code=404, detail="shared set not found")
    return HTMLResponse(_render(session), headers={"cache-control": "no-cache"})


__all__ = ["router"]
