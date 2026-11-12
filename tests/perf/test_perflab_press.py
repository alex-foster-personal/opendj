"""press.mjs: latency presses through the page's real controls (LATENCY-01).

A small real page stands in for /performance: the same data-testids, ARIA
attributes and gestures the components render (TransportCluster, VFader,
Knob), handlers that reach a dispatcher, and the browser IPC's query. The
press drives it with real Playwright pointer input in a real Chromium.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.perf.perflab_support import LAB, _chromium, _load, _node_json

# The stand-in app. Every control dispatches from its own handler, after
# window.__handlerDelayMs (a delay before the dispatcher, as a slow handler
# would add) and window.__appWorkMs of busy work in the handler (the app's
# own main-thread work, which a press window must still attribute).
_APP = r"""
<html><body style="margin:0">
<style>
  .c { position: absolute; top: 20px; background: #999; }
</style>
<button data-testid="play-deck-1" aria-pressed="false" class="c" style="left:20px;width:60px;height:30px">play</button>
<div data-testid="channel-1-fader" role="slider" aria-valuenow="1" class="c" style="left:120px;width:30px;height:120px"></div>
<div id="paint" style="position:absolute;left:0;top:200px;width:300px;height:80px;overflow:hidden"></div>
<div data-testid="knob-1:low" role="slider" aria-valuenow="0.5" class="c" style="left:200px;width:40px;height:40px"></div>
<script>
const state = { decks: { 1: { playing: false, transport_pending: false } }, mixer: { channels: { 1: { fader: 1, eq_low: 0.5 } } } };
const el = (id) => document.querySelector(`[data-testid="${id}"]`);
function render() {
  if (window.__noRender) return;
  // planted paint cost in the frame that shows the change: a large box-shadow repaint
  // (its cost is in the paint after the write, which the press window must include)
  const paint = document.getElementById('paint');
  if (window.__paintMs && paint.childElementCount === 0) {
    for (let i = 0; i < 5000; i++) {
      const d = document.createElement('i');
      d.style.cssText = 'display:inline-block;width:6px;height:6px;box-shadow:0 0 6px #000';
      paint.appendChild(d);
    }
  }
  if (window.__paintMs) paint.classList.toggle('on');
  if (window.__renderMs) { const t = performance.now(); while (performance.now() - t < window.__renderMs); }
  if (window.__extraFrameMs) requestAnimationFrame(() => { const t = performance.now(); while (performance.now() - t < window.__extraFrameMs); });
  el('play-deck-1').setAttribute('aria-pressed', String(state.decks[1].playing));
  if (el('channel-1-fader')) el('channel-1-fader').setAttribute('aria-valuenow', String(state.mixer.channels[1].fader));
  el('knob-1:low').setAttribute('aria-valuenow', String(state.mixer.channels[1].eq_low));
}
async function dispatch(c) {
  if (c.type === 'play') state.decks[1].playing = c.playing;
  else if (c.type === 'fader') state.mixer.channels[1].fader = c.value;
  else if (c.type === 'eq') state.mixer.channels[1].eq_low = c.value;
  requestAnimationFrame(render);
}
window.musicDjToolsPerformance = { dispatch, query: () => JSON.parse(JSON.stringify(state)) };
function perflabTestAppWork() {
  const t = performance.now();
  let x = 0;
  while (performance.now() - t < (window.__appWorkMs ?? 0)) x += Math.sqrt(x + 1);
  return x;
}
async function handler(cmd) {
  if (window.__handlerDelayMs) await new Promise((r) => setTimeout(r, window.__handlerDelayMs));
  perflabTestAppWork();
  await dispatch(cmd);
}
const clamp = (v) => Math.min(1, Math.max(0, v));
el('play-deck-1').addEventListener('click', () => handler({ type: 'play', playing: !state.decks[1].playing }));
const fader = el('channel-1-fader');
fader.addEventListener('pointerdown', (e) => {
  const r = fader.getBoundingClientRect();
  handler({ type: 'fader', value: clamp(1 - (e.clientY - r.top - 6) / (r.height - 12)) });
});
const knob = el('knob-1:low');
let drag = null;
knob.addEventListener('pointerdown', (e) => { drag = { y: e.clientY, v: state.mixer.channels[1].eq_low }; knob.setPointerCapture(e.pointerId); });
knob.addEventListener('pointermove', (e) => { if (drag) handler({ type: 'eq', value: clamp(drag.v + (drag.y - e.clientY) / 120) }); });
knob.addEventListener('pointerup', () => { drag = null; });
knob.addEventListener('dblclick', () => handler({ type: 'eq', value: 0.5 }));
</script></body></html>
"""

_PRELUDE = """
import path from 'node:path';
import { createRequire } from 'node:module';
const [lab, chrome, app, plan] = process.argv.slice(1);
const { PRESS_INIT, press } = await import(path.join(lab, 'press.mjs'));
const { chromium } = createRequire(path.resolve(lab, '../../../apps/webui/frontend/package.json'))('playwright');
const browser = await chromium.launch({ executablePath: chrome });
const ctx = await browser.newContext({ viewport: { width: 400, height: 300 } });
await ctx.addInitScript({ content: PRESS_INIT });
const page = await ctx.newPage();
const opts = JSON.parse(plan);
await page.setContent(app);
if (opts.dropFader) await page.evaluate(() => document.querySelector('[data-testid="channel-1-fader"]').remove());
const k = { deck: 1, FADER_OPEN: 1, EQ_UNITY: 0.5 };
const setPlaying = (playing) => page.evaluate((p) => window.musicDjToolsPerformance.dispatch({ type: 'play', playing: p }), playing);
"""

# Budget mode: 100 timed play presses (the state is reset between them by the
# dispatcher, which is setup, not the press), then one of each other control.
_BUDGET = (
    _PRELUDE
    + """
await page.evaluate((ms) => { window.__handlerDelayMs = ms; }, opts.delayMs);
const rows = [];
for (let i = 0; i < opts.reps; i++) {
  await setPlaying(false);
  await page.waitForTimeout(20);
  rows.push(await press(page, { action: 'play', ...k }));
}
await setPlaying(false);
for (const action of ['fader_cut', 'fader_open', 'eq_low_kill', 'eq_low_restore']) rows.push(await press(page, { action, ...k }));
console.log(JSON.stringify({ rows }));
await browser.close();
"""
)


def _rows(delay_ms: int, reps: int = 100, drop_fader: bool = False) -> list[dict]:
    plan = json.dumps({"delayMs": delay_ms, "reps": reps, "dropFader": drop_fader})
    return _node_json(_BUDGET, str(LAB), _chromium(), _APP, plan, timeout=300)["rows"]


def _summary(tmp_path: Path, rows: list[dict]) -> dict:
    (tmp_path / "latency.json").write_text(json.dumps({"rows": rows}), encoding="utf-8")
    return _load("analyze").latency_attribution(None, tmp_path)["summary"]


def test_a_delay_in_a_controls_handler_moves_the_press_p99(tmp_path: Path) -> None:
    """Every press is a real pointer gesture on the control, timed from its
    input event: a 60 ms delay in the play button's handler, before it reaches
    the dispatcher, moves the reflected and next-frame p99 by about 60 ms. A
    press that called the dispatcher itself would not see it. Control: with no
    delay the same p99s stay well under it, and every other control (fader cut
    and open, low knob drag to kill, double-click restore) is reached by its own
    gesture and renders its value."""
    base = _rows(0)
    slow = _rows(60)
    for rows in (base, slow):
        assert all(r["press_error"] is None for r in rows), [r["press_error"] for r in rows if r["press_error"]]
    others = {r["action"]: r for r in base[100:]}
    for action, event in (
        ("fader_cut", "pointerdown"),
        ("fader_open", "pointerdown"),
        ("eq_low_kill", "pointermove"),
        ("eq_low_restore", "dblclick"),
    ):
        assert others[action]["input"]["event"] == event and others[action]["rendered_ms"] is not None, others[action]
    b, s = _summary(tmp_path, base)["play"], _summary(tmp_path, slow)["play"]
    assert b["status"] == "OK" and b["reflected_ms_p99"] is not None and b["next_frame_ms_p99"] is not None, b
    assert b["reflected_ms_p99"] < 30, b
    assert s["reflected_ms_p99"] - b["reflected_ms_p99"] >= 45, (b, s)
    assert s["next_frame_ms_p99"] - b["next_frame_ms_p99"] >= 45, (b, s)


def test_a_control_with_no_element_is_unknown_never_a_dispatch(tmp_path: Path) -> None:
    """With the fader gone from the page, its presses name the missing control
    and measure nothing, and the action reads UNKNOWN; the knob beside it is
    still pressed (control)."""
    rows = _rows(0, reps=1, drop_fader=True)
    by = {r["action"]: r for r in rows}
    for action in ("fader_cut", "fader_open"):
        assert "control channel-1-fader: 0 elements" in by[action]["press_error"], by[action]
        assert by[action]["reflected_ms"] is None and by[action]["next_frame_ms"] is None
    assert by["eq_low_kill"]["press_error"] is None and by["eq_low_kill"]["rendered_ms"] is not None
    summary = _summary(tmp_path, rows)
    assert summary["fader_cut"]["status"] == "UNKNOWN" and summary["eq_low_kill"]["status"] == "OK", summary


# Attribution mode under a real trace: the app's handler burns 25 ms of
# planted work per press, and the harness's own render check (the injected
# __perflabRenderedControl, replaced here to burn 30 ms first) is planted
# harness-only work inside the window.
_TRACED = (
    _PRELUDE
    + """
import fs from 'node:fs';
const cdp = await ctx.newCDPSession(page);
await page.evaluate(() => {
  window.__appWorkMs = 25;
  // count the observers the press creates and whether each is still connected
  window.__obsCreated = 0;
  window.__obsLive = new Set();
  const RealObserver = window.MutationObserver;
  // only attribute observers of the page count (the press's); a browser-side
  // helper's own observers are not the press's to leave behind
  window.MutationObserver = class extends RealObserver {
    observe(target, options) {
      if (options && options.attributes) { window.__obsCreated++; window.__obsLive.add(this); }
      return super.observe(target, options);
    }
    disconnect() { window.__obsLive.delete(this); super.disconnect(); }
  };
  // planted unrelated work in the frame AFTER the change is painted: a press window
  // that runs one frame too long attributes it to the press
  window.__extraFrameMs = 40;
  // planted work in the frame that SHOWS the change, before its write: the press
  // window must include it (the real painted frame counts)
  window.__renderMs = 15;
  // planted paint in the showing frame, after the write
  window.__paintMs = 1;
  document.head.insertAdjacentHTML('beforeend', '<style>#paint.on i { box-shadow: 0 0 18px 6px #f00; filter: blur(1px); }</style>');
  // The harness's planted work: 30 ms per press, in the capture-phase click
  // listener, before the app's handler and frame, measured as perflab:probe.
  // It is inside the press window (which starts at the click's timestamp) and
  // no poll of the press is slowed by it.
  document.addEventListener('click', () => {
    const s = performance.now();
    let x = 0; while (performance.now() - s < 30) x += Math.sqrt(x + 1);
    performance.measure('perflab:probe', { start: s, end: performance.now() });
  }, true);
});
const categories = ['toplevel', 'blink', 'blink.user_timing', 'devtools.timeline', 'v8', 'v8.execute', 'input', 'cc'];
categories.push('disabled-by-default-devtools.timeline');
await cdp.send('Tracing.start', { transferMode: 'ReturnAsStream', streamFormat: 'proto', traceConfig: { includedCategories: categories } });
const rows = [];
for (let i = 0; i < opts.reps; i++) {
  rows.push(await press(page, { action: 'play', ...k, attribution: true }));
  rows.push(await press(page, { action: 'pause', ...k, attribution: true }));
}
rows.push(await press(page, { action: 'eq_low_kill', ...k, attribution: true }));
rows.push(await press(page, { action: 'idle_control', ...k, attribution: true }));
const obs = await page.evaluate(() => ({ created: window.__obsCreated, live: window.__obsLive.size }));
const done = new Promise((r) => cdp.once('Tracing.tracingComplete', r));
await cdp.send('Tracing.end');
const { stream } = await done;
const fd = fs.openSync(opts.trace, 'w');
for (;;) {
  const c = await cdp.send('IO.read', { handle: stream, size: 8 << 20 });
  fs.writeSync(fd, c.base64Encoded ? Buffer.from(c.data, 'base64') : Buffer.from(c.data, 'binary'));
  if (c.eof) break;
}
fs.closeSync(fd);
console.log(JSON.stringify({ rows, obs }));
await browser.close();
"""
)

_ATTRIBUTE = """
import importlib.util, json, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("perflab_analyze", sys.argv[1])
analyze = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analyze)
tp = analyze.open_trace(Path(sys.argv[2]))
assert tp is not None, "no trace.pftrace"
attributed, harness = analyze._press_window_attribution(tp)
windows = analyze.q(tp, "select name, dur from slice where name like 'perflab:latency:%' and dur > 0")
print(json.dumps({"attributed": attributed, "harness": harness, "windows": windows}))
"""


def _attribute(trace: Path) -> dict:
    """analyze.py's press-window attribution of a real trace, under the
    perfetto trace processor (an offline uv environment), or UNAVAILABLE."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("UNAVAILABLE: uv is not on PATH to run analyze.py's perfetto environment")
    argv = [uv, "run", "--offline", "--no-project", "--with", "perfetto>=0.11", "--with", "psutil", "python", "-I"]
    r = subprocess.run(
        [*argv, "-c", _ATTRIBUTE, str(LAB / "analyze.py"), str(trace.parent)],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
        env={k: v for k, v in os.environ.items() if not k.startswith("UV_")} | {"PATH": os.environ["PATH"]},
    )
    if r.returncode != 0 and "No solution found" in r.stderr:
        pytest.skip(f"UNAVAILABLE: perfetto is not in the offline uv cache: {r.stderr[-300:]}")
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_press_windows_attribute_the_apps_work_and_not_the_harness_probe(tmp_path: Path) -> None:
    """A real Chromium trace of attribution-mode presses: the app's planted
    25 ms per press is attributed to the press (script), while the harness's
    planted 30 ms per render check, inside the same window, is not, and is
    reported apart as harness-probe. The window closes at the painted frame
    showing the effect: an eq_low_kill window is a few frames, never the
    1.5 s the budget pass's detectors and series run, and its row carries no
    detector."""
    reps = 4
    trace = tmp_path / "trace.pftrace"
    plan = json.dumps({"delayMs": 0, "reps": reps, "trace": str(trace)})
    traced = _node_json(_TRACED, str(LAB), _chromium(), _APP, plan, timeout=300)
    rows, obs = traced["rows"], traced["obs"]
    pressed = [r for r in rows if r["action"] != "idle_control"]
    assert all(r["press_error"] is None and r["rendered_ms"] is not None for r in pressed), rows
    kill = rows[-2]
    assert kill["detectors_usable"] == [] and kill["low_ratio_series"] is None, kill
    # observers: one per commanded press, and none left connected after the pass
    assert obs == {"created": 2 * reps + 1, "live": 0}, obs
    out = _attribute(trace)
    presses = {"play": reps, "pause": reps}
    for action, n in presses.items():
        script = out["attributed"].get(action, {}).get("script", 0.0)
        harness = out["harness"].get(action, 0.0)
        assert script >= 0.8 * (25 + 15) * n, (action, out)  # the app's work is the press's
        assert script < (25 + 15) * n + 0.5 * 30 * n, (
            action,
            out,
        )  # the harness's is not, nor the frame after the change
        assert harness >= 0.8 * 30 * n, (action, out)  # and is reported apart
    # the planted paint (5000 shadowed boxes restyled in the showing frame, after
    # the write) is style and paint time inside the play window: a window that
    # ends at the write holds almost none of it
    paint = {k: out["attributed"].get("play", {}).get(k, 0.0) for k in ("style", "paint", "layerize-commit")}
    assert sum(paint.values()) >= 10 * reps, (paint, out["attributed"].get("play"))
    kills = [w["dur"] / 1e6 for w in out["windows"] if w["name"] == "perflab:latency:eq_low_kill"]
    assert kills and max(kills) < 500, out["windows"]


_NO_RENDER = (
    _PRELUDE
    + """
await page.evaluate(() => {
  window.__noRender = true;
  window.__obsCreated = 0;
  window.__obsLive = new Set();
  const RealObserver = window.MutationObserver;
  window.MutationObserver = class extends RealObserver {
    observe(target, options) {
      if (options && options.attributes) { window.__obsCreated++; window.__obsLive.add(this); }
      return super.observe(target, options);
    }
    disconnect() { window.__obsLive.delete(this); super.disconnect(); }
  };
});
const row = await press(page, { action: 'play', ...k, attribution: true });
const obs = await page.evaluate(() => ({ created: window.__obsCreated, live: window.__obsLive.size }));
console.log(JSON.stringify({ row, obs }));
await browser.close();
"""
)


def test_a_press_whose_control_never_renders_leaves_no_observer(tmp_path: Path) -> None:
    """The write never comes (the app does not render the control), so the press
    gives up after its wait and must still disconnect the observer it armed."""
    out = _node_json(
        _NO_RENDER,
        str(LAB),
        _chromium(),
        _APP,
        json.dumps({"delayMs": 0, "reps": 1, "trace": str(tmp_path / "t")}),
        timeout=300,
    )
    assert out["row"]["press_error"] is None and out["row"]["rendered_ms"] is None, out["row"]
    assert out["obs"] == {"created": 1, "live": 0}, out["obs"]


def test_driver_presses_only_through_press_mjs() -> None:
    """driver.mjs's latency loop presses through press.mjs, so no press path
    calls the browser IPC's dispatch: setup commands (seek, fader, EQ, sync)
    stay on ipc(), presses never do."""
    src = (LAB / "driver.mjs").read_text(encoding="utf-8")
    lat = src[src.index("async function scenarioLatency(") : src.index("async function scenarioLeak(")]
    assert "const row = await press(page, { action, deck, FADER_OPEN, EQ_UNITY, attribution: !budget });" in lat
    assert "ipc.dispatch" not in lat and "dispatch(cmd" not in lat
    press = (LAB / "press.mjs").read_text(encoding="utf-8")
    assert "dispatch" not in press.split("async function pressInPage(", 1)[1].replace("dispatcher", "")


# STRUCTURAL check (not a measurement): the press window's end must be a task
# posted from the MutationObserver callback that sees the write. The paint-plant
# test above cannot tell a rAF-timestamp endpoint from a post-paint one, because
# a rAF registered in the writing frame gets the NEXT frame's begin time, which is
# after the writing frame's paint, and it measures more work, not less
# (play window style 281/paint 86/layerize 14 ms vs post-paint 255/76/31 ms).
# So the endpoint is checked by its source shape, and the checker is itself
# checked against mutations of that source.
_PRESS_SRC = LAB / "press.mjs"


def _endpoint_body(src: str) -> str:
    start = src.index("const renderedAfterPaint")
    return src[start : src.index("return { promise, dispose };", start)]


def _endpoint_violations(body: str) -> list[str]:
    out = []
    if "new MutationObserver(" not in body:
        out.append("no MutationObserver")
    if "requestAnimationFrame" in body:
        out.append("a rAF timestamp is used in the endpoint path")
    if "new MessageChannel()" not in body or "port1.onmessage" not in body:
        out.append("the endpoint is not a MessageChannel task")
    if "resolve(performance.now())" not in body.split("port1.onmessage", 1)[-1][:200]:
        out.append("the endpoint time is not taken inside the posted task")
    return out


def test_the_press_endpoint_is_a_task_posted_from_the_observer() -> None:
    body = _endpoint_body(_PRESS_SRC.read_text())
    assert _endpoint_violations(body) == [], body


def test_the_endpoint_check_goes_red_on_each_mutation() -> None:
    good = _endpoint_body(_PRESS_SRC.read_text())
    task = (
        "const c = new MessageChannel();\n"
        "\t\t\t\tc.port1.onmessage = () => { c.port1.close(); resolve(performance.now()); };\n"
        "\t\t\t\tc.port2.postMessage(0);"
    )
    assert task in good, "the structural anchor moved; update the mutations"
    rafts = "requestAnimationFrame((ts) => resolve(ts));"
    at_mutation = "resolve(performance.now());"
    assert _endpoint_violations(good.replace(task, rafts)), "rAF endpoint not caught"
    assert _endpoint_violations(good.replace(task, at_mutation)), "mutation-time endpoint not caught"


_BOUNDARY = (
    _PRELUDE
    + """
await page.evaluate(() => {
  window.__writeBoundary = null;
  const RealObserver = window.MutationObserver;
  // A probe rAF is registered from the observer callback, only when the
  // control already shows the new state, so it belongs to the frame after the
  // writing frame. Its callback time is that frame's begin, the boundary.
  window.MutationObserver = class extends RealObserver {
    constructor(cb) {
      super((recs, o) => {
        if (window.__writeBoundary === null && window.__perflabRenderedControl('play', 1, null)) {
          requestAnimationFrame((ts) => { if (window.__writeBoundary === null) window.__writeBoundary = ts; });
        }
        cb(recs, o);
      });
    }
  };
});
const row = await press(page, { action: 'play', ...k, attribution: true });
// the probe's frame runs after the press returns: wait for its callback
await page.waitForFunction(() => window.__writeBoundary !== null, null, { timeout: 2000 }).catch(() => {});
const boundary = await page.evaluate(() => window.__writeBoundary);
console.log(JSON.stringify({ row, boundary }));
await browser.close();
"""
)


def test_boundary_the_window_ends_before_the_frame_after_the_write_begins(tmp_path: Path) -> None:
    """Boundary assertion (structural-in-the-browser, not a paint measurement).
    The window ends strictly before the begin time of the frame after the one
    that writes the control, which is the rAF begin time of a probe registered
    from the observer callback. A rAF timestamp used as the endpoint is that
    same begin time, so it fails the strict bound. Numbers from one trace each:
    post-paint play window style 255 / paint 76 / layerize 31 ms; rAF-timestamp
    endpoint style 281 / paint 86 / layerize 14 ms (it measures more work)."""
    out = _node_json(
        _BOUNDARY,
        str(LAB),
        _chromium(),
        _APP,
        json.dumps({"delayMs": 0, "reps": 1, "trace": str(tmp_path / "t")}),
        timeout=300,
    )
    row, boundary = out["row"], out["boundary"]
    assert row["press_error"] is None and row["next_frame_ms"] is not None, row
    assert boundary is not None, out
    end = row["t0"] + row["next_frame_ms"]
    assert end < boundary, (end, boundary, row["next_frame_ms"])
