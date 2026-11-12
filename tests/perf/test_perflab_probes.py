"""perf-lab probes in a real Chromium and real node: the destination probe and
health monitor hear real audio (and a cut deck), the painted-frame clock lands
after rendering work, presentation waits for the output clock, and the heap
diff finds a planted leak in real V8 snapshots while churn drops out."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.perf.perflab_support import LAB, _chromium, _node, _node_json

_PRELUDE = """
import path from 'node:path';
import { createRequire } from 'node:module';
const [lab, chrome] = process.argv.slice(1);
const { installAudioProbe, destinationLevelDbInPage, silentReason } = await import(path.join(lab, 'audio-probe.mjs'));
const health = await import(path.join(lab, 'audio-health.mjs'));
const { paintedFrame } = await import(path.join(lab, 'frame-clock.mjs'));
const { chromium } = createRequire(path.resolve(lab, '../../../apps/webui/frontend/package.json'))('playwright');
const browser = await chromium.launch({ executablePath: chrome, args: ['--autoplay-policy=no-user-gesture-required'] });
const ctx = await browser.newContext();
await ctx.addInitScript(installAudioProbe);
const page = await ctx.newPage();
await page.setContent('<html><body></body></html>');
"""

_DESTINATION = (
    _PRELUDE
    + """
await page.evaluate(() => {
  const ac = new AudioContext();
  const osc = new OscillatorNode(ac, { frequency: 440 });
  const master = new GainNode(ac, { gain: 0.3 });
  osc.connect(master); master.connect(ac.destination); osc.start();
  window.__t = { ac, master };
  return ac.resume();
});
// a graph change reaches the analyser once the audio thread renders it: poll until it settles
const level = async (silent) => {
  let r;
  for (const t0 = Date.now(); Date.now() - t0 < 5000; ) {
    await page.waitForTimeout(100);
    r = silentReason(await page.evaluate(destinationLevelDbInPage, 200));
    if ((r !== null) === silent) break;
  }
  return r;
};
const out = { playing: await level(false) };
await page.evaluate(() => { window.__t.master.gain.value = 0; });
out.muted = await level(true);
await page.evaluate(() => { window.__t.master.gain.value = 0.3; });
out.restored = await level(false);
await page.evaluate(() => { window.__t.master.disconnect(window.__t.ac.destination); });
out.cut = await level(true);
// 60 ms of rendering work in the frame, registered after the clock's rAF
out.frame = await page.evaluate(async (src) => {
  const painted = eval(`(${src})`);
  const t0 = performance.now();
  const single = new Promise((r) => requestAnimationFrame(() => r(performance.now())));
  const p = painted();
  requestAnimationFrame(() => { const t = performance.now(); while (performance.now() - t < 60); });
  return { single: (await single) - t0, painted: (await p) - t0 };
}, paintedFrame.toString());
console.log(JSON.stringify(out));
await browser.close();
"""
)


def test_destination_probe_hears_real_audio_and_the_painted_frame_follows_paint() -> None:
    out = _node_json(_DESTINATION, str(LAB), _chromium())
    assert out["playing"] is None and out["restored"] is None  # controls: real audio is heard
    assert out["muted"].startswith("destination silent") and out["cut"].startswith("destination silent"), out
    assert out["frame"]["single"] < 60 <= out["frame"]["painted"], out["frame"]


_MIX = (
    _PRELUDE
    + """
// four oscillator decks, each through its own fader into the master, plus an
// idle four-input cue bus that also reaches the destination; driven through
// the same query()/dispatch() contract the app's performance surface exposes
await page.evaluate(() => {
  const ac = new AudioContext();
  const master = new GainNode(ac, { gain: 0.25 });
  master.connect(ac.destination);
  const cue = new GainNode(ac);
  cue.connect(ac.destination);
  const faders = [220, 330, 440, 550].map((f) => {
    const o = new OscillatorNode(ac, { frequency: f });
    const g = new GainNode(ac, { gain: 0.8 });
    const c = new GainNode(ac, { gain: 0 });
    o.connect(g); g.connect(master); o.connect(c); c.connect(cue); o.start();
    return g;
  });
  window.__mix = { faders, master };
  const deck = { playing: true, audible: true, transport_pending: false, transport_clock: { desired_revision: 1, presented_revision: 1 } };
  window.__deck = deck;
  window.musicDjToolsPerformance = {
    query: () => ({ decks: { 1: deck, 2: deck, 3: deck, 4: deck },
      mixer: { channels: Object.fromEntries(faders.map((g, i) => [i + 1, { fader: g.gain.value }])) } }),
    dispatch: async (c) => { if (c.type === 'fader') faders[c.deck - 1].gain.value = c.value; },
  };
  return ac.resume();
});
const watch = async (cut) => {
  const settled = await health.awaitHealthyDecks(page, 4);
  const m = await health.monitorAudio(page, 4, 250);
  if (cut) {
    await page.waitForTimeout(600);
    await page.evaluate(cut);
    await page.waitForTimeout(1200);
    await page.evaluate(() => { const { faders, master } = window.__mix; faders[2].gain.setValueAtTime(0.8, 0); faders[2].connect(master); });
    await page.waitForTimeout(800);
  } else await page.waitForTimeout(2600);
  const samples = await m.stop();
  return { settled, problems: health.windowAudioProblems(samples, 3), end: await health.unhealthyDecks(page, 4) };
};
const out = { clean: await watch(null) };
out.silenced = await watch(() => window.__mix.faders[2].gain.setValueAtTime(0, 0));
// the in-window watch is passive: once it is running, nothing crosses CDP
// into the measured page until stop() collects its buckets (counted, not stubbed:
// every evaluate still runs)
{
  const real = page.evaluate.bind(page);
  let calls = 0;
  page.evaluate = (...a) => { calls++; return real(...a); };
  const m = await health.monitorAudio(page, 4, 250);
  const started = calls;
  await page.waitForTimeout(2500);
  const during = calls - started;
  const samples = await m.stop();
  page.evaluate = real;
  out.passive = { during, samples: samples.length, problems: health.windowAudioProblems(samples, 4) };
}
// a dropout far shorter than the 1 s check interval: one span of the passive
// watch fails, though the interval's loudest reads stay healthy
const brief = async (cut, restore) => {
  const m = await health.monitorAudio(page, 4, 1000);
  await page.waitForTimeout(1200);
  await page.evaluate(cut);
  await page.waitForTimeout(400);
  await page.evaluate(restore);
  await page.waitForTimeout(1600);
  return health.windowAudioProblems(await m.stop(), 2);
};
out.briefNone = await brief(() => {}, () => {});
out.briefDrop = await brief(
  () => window.__mix.faders[2].gain.setValueAtTime(0, 0),
  () => window.__mix.faders[2].gain.setValueAtTime(0.8, 0)
);
// the master scheduled off and on every 130 ms: every 200 ms span's loudest
// read still hears it, but each 130 ms gap holds two quiet reads 50 ms apart
out.pulsing = await brief(
  () => {
    const g = window.__mix.master.gain;
    const t = window.__mix.master.context.currentTime;
    for (let i = 0; i < 30; i++) g.setValueAtTime(i % 2 ? 0.25 : 0, t + i * 0.13);
  },
  () => {}
);
await page.evaluate(() => {
  const g = window.__mix.master.gain;
  g.cancelScheduledValues(0);
  g.value = 0.25;
});
// one deck's fader at 0 for 120 ms on the audio clock, mid-window, then back:
// the dropout covers part of a 200 ms span and resumes inside the window
const midDrop = async (spanMs, ms) => {
  const m = await health.monitorAudio(page, 4, 1000, 50, spanMs);
  await page.waitForTimeout(1300);
  await page.evaluate((ms) => {
    const g = window.__mix.faders[2].gain;
    const t = window.__mix.master.context.currentTime + 0.02;
    g.setValueAtTime(0, t);
    g.setValueAtTime(0.8, t + ms / 1000);
  }, ms);
  await page.waitForTimeout(1500);
  const samples = await m.stop();
  await page.evaluate(() => { const g = window.__mix.faders[2].gain; g.cancelScheduledValues(0); g.value = 0.8; });
  return { problems: health.windowAudioProblems(samples, 2), quiet: samples.reduce((t, x) => t + (x.quiet ?? 0), 0) };
};
out.midDrop = await midDrop(200, 120);
// every span one read long, so the two quiet reads of the dropout sit in two
// spans: only a run carried across the span edge catches it
out.edgeDrop = await midDrop(1, 120);
out.midNone = await midDrop(200, 0);
// isolated quiet reads: 30 ms gaps every 130 ms on one deck. The period is not
// a multiple of the 50 ms read cadence, so the reads sweep every phase of it
// and some land quiet in a gap, but two reads 50 ms apart never both do
const gaps = async () => {
  const m = await health.monitorAudio(page, 4, 1000);
  await page.evaluate(() => {
    const g = window.__mix.faders[2].gain;
    const t = window.__mix.master.context.currentTime + 0.02;
    for (let i = 0; i < 30; i++) { g.setValueAtTime(0, t + i * 0.13); g.setValueAtTime(0.8, t + i * 0.13 + 0.03); }
  });
  await page.waitForTimeout(3200);
  const samples = await m.stop();
  await page.evaluate(() => { const g = window.__mix.faders[2].gain; g.cancelScheduledValues(0); g.value = 0.8; });
  return { problems: health.windowAudioProblems(samples, 2), quiet: samples.reduce((t, x) => t + (x.quiet ?? 0), 0) };
};
out.gaps = await gaps();
out.briefState = await brief(() => { window.__deck.audible = false; }, () => { window.__deck.audible = true; });
// stop() lands inside a span: one 1 s span closes healthy, and a dropout
// fills the second, partial one, which only stop() can close (it recovers
// after the window, where an endpoint check made then would pass it)
const tail = async (cut, restore) => {
  const m = await health.monitorAudio(page, 4, 1000, 50, 1000);
  await page.waitForTimeout(900);
  await page.evaluate(cut);
  await page.waitForTimeout(500);
  const problems = health.windowAudioProblems(await m.stop(), 1);
  await page.evaluate(restore);
  return problems;
};
out.tailNone = await tail(() => {}, () => {});
// set plainly, with no automation left behind for the solo checks below
out.tailDrop = await tail(
  () => { const g = window.__mix.faders[2].gain; g.cancelScheduledValues(0); g.value = 0; },
  () => { const g = window.__mix.faders[2].gain; g.cancelScheduledValues(0); g.value = 0.8; }
);
await page.waitForTimeout(200);
out.solo = await health.decksNotReachingOutput(page, 4);
await page.evaluate(() => window.__mix.faders[1].disconnect());
out.soloCut = await health.decksNotReachingOutput(page, 4);
out.faders = await page.evaluate(() => window.__mix.faders.map((g) => +g.gain.value.toFixed(3)));
await page.evaluate(() => window.__mix.faders[1].connect(window.__mix.master));
// a dispatcher that records the fader but never reaches the graph: soloing hears the whole mix
await page.evaluate(() => { window.musicDjToolsPerformance.dispatch = async () => {}; });
out.inertDispatch = await health.decksNotReachingOutput(page, 4);
out.unwatched = health.windowAudioProblems([], 3);
// loaded decks read through the same query(): deck 2's processor died, so it
// lost its track and carries the error until the next load clears it
await page.evaluate(() => {
  const loaded = { stable_id: 'a', playing: false, transport_pending: false, processor_error: null };
  window.musicDjToolsPerformance.query = () => ({ decks: { 1: loaded, 2: { ...loaded, stable_id: null, processor_error: 'worklet died' } } });
});
out.loadedOk = await health.loadedDecksFaults(page, ['a']);
out.loadedDead = await health.loadedDecksFaults(page, ['a', 'b']);
console.log(JSON.stringify(out));
await browser.close();
"""
)


def test_one_deck_silenced_inside_a_window_voids_it_though_the_mix_stays_loud() -> None:
    """Four real Web Audio decks behind a stand-in of the performance surface's
    query()/dispatch() contract (the probe's unit; the driver runs it against
    the app's own dispatcher): one silenced mid-window while three keep the
    destination loud is caught by the per-input mix check, though both
    endpoint checks pass; the solo check names a deck cut from the mix,
    restores every fader, and refuses a dispatcher that never reaches the
    graph. A clean window passes (control) and an unwatched one never does.
    A dropout of about 120 ms that covers part of a 200 ms span and resumes is
    caught by the run of consecutive quiet reads, also when the run crosses a
    span edge, while continuous audio and isolated quiet reads still pass.
    The watch inside a window is passive: no CDP call reaches the measured page
    between its start and stop(), so its cost is not charged to the window."""
    out = _node_json(_MIX, str(LAB), _chromium())
    assert out["clean"]["settled"] == [] and out["clean"]["problems"] == [], out  # control
    # the watch inside a window makes no CDP call into the page it measures, and
    # still closes a bucket per check interval
    assert out["passive"]["during"] == 0 and out["passive"]["samples"] >= 5, out["passive"]
    assert out["passive"]["problems"] == [], out["passive"]
    # a dropout that covers part of a span and resumes leaves the span's loudest
    # read healthy; the run of consecutive quiet reads catches it, for the
    # destination (the master gated every 130 ms) and for one deck's input
    assert "consecutive quiet reads" in out["pulsing"][0], out["pulsing"]
    assert "consecutive quiet reads" in out["midDrop"]["problems"][0], out["midDrop"]
    assert out["midDrop"]["quiet"] >= 2, out["midDrop"]
    # the run is carried across span edges: with one read per span it still fails
    assert "consecutive quiet reads" in out["edgeDrop"]["problems"][0], out["edgeDrop"]
    # the other direction: continuous audio passes, and so do isolated quiet reads
    assert out["midNone"] == {"problems": [], "quiet": 0}, out["midNone"]  # control
    assert out["gaps"]["problems"] == [] and out["gaps"]["quiet"] >= 1, out["gaps"]
    # a dropout of a few hundred ms inside a 1 s interval fails it, by level and by
    # deck state alike, though the interval's loudest reads stay healthy
    assert out["briefNone"] == [], out["briefNone"]  # control: the same timing, nothing cut
    assert "only 3 of 4 deck inputs" in out["briefDrop"][0], out["briefDrop"]
    assert "not audible" in out["briefState"][0], out["briefState"]
    # a dropout in the partial span stop() lands in is judged too
    assert out["tailNone"] == [], out["tailNone"]  # control: the same timing, nothing cut
    assert "only 3 of 4 deck inputs" in out["tailDrop"][0], out["tailDrop"]
    assert out["silenced"]["end"] == [], out  # the endpoint check alone passes it
    assert "only 3 of 4 deck inputs" in out["silenced"]["problems"][0], out
    assert out["solo"] == [], out  # control: every deck reaches the output on its own
    assert len(out["soloCut"]) == 1 and out["soloCut"][0].startswith("deck 2 solo: destination silent"), out
    assert out["faders"] == [0.8, 0.8, 0.8, 0.8]
    # the probe validates its own instrument: faders that never reach the graph are named, not passed
    assert len(out["inertDispatch"]) == 1 and "does not reach the audio graph" in out["inertDispatch"][0], out
    assert "fewer than 3" in out["unwatched"][0]
    assert out["loadedOk"] == [], out  # control: a stopped deck holding its track is loaded
    assert out["loadedDead"] == ["deck 2: holds nothing, expected b, processor_error=worklet died"], out


_CLOCKS = """
import path from 'node:path';
const lab = process.argv[1];
const { presentedStamp: p, presentedAt } = await import(path.join(lab, 'output-clock.mjs'));
const { loadedReason, unhealthyReason } = await import(path.join(lab, 'audio-health.mjs'));
const { captureFaults } = await import(path.join(lab, 'capture-meta.mjs'));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const run = (stamps, ctxTime) => { let i = 0; return presentedAt(() => stamps(i++), ctxTime, sleep, 200); };
const ok = { playing: true, audible: true, transport_pending: false, processor_error: null,
  transport_clock: { desired_revision: 7, presented_revision: 7 } };
console.log(JSON.stringify({
  presented: [p({ contextTime: 1.5, performanceTime: 0 }, 1.2), p({ contextTime: 0, performanceTime: 9 }, 0), p({ contextTime: 1.1 }, 1.2)],
  stalled: await run(() => ({ contextTime: 1.0, performanceTime: 5000 }), 1.5),
  reached: await run((i) => ({ contextTime: 1.0 + 0.2 * i, performanceTime: 5000 + 200 * i }), 1.5),
  observed: await run((i) => ({ contextTime: 1.0 + 0.2 * i, performanceTime: null }), 1.5),
  health: [unhealthyReason(ok), unhealthyReason({ ...ok, audible: false }),
    unhealthyReason({ ...ok, transport_clock: { desired_revision: 8, presented_revision: 7 } })],
  loaded: [loadedReason({ stable_id: 'a', playing: false, transport_pending: false, processor_error: null }, 'a'),
    loadedReason({ stable_id: 'b', transport_pending: false, processor_error: null }, 'a'),
    loadedReason({ stable_id: 'a', transport_pending: false, processor_error: 'decode failed' }, 'a'),
    loadedReason({ stable_id: 'a', transport_pending: true, processor_error: null }, 'a'), loadedReason(null, 'a')],
  faults: [captureFaults(), captureFaults({ latencyRows: [{ action: 'eq_low_restore', press_error: 'refused' }],
    finalQuery: { 3: { processor_error: 'decode failed' } } })],
}));
"""


def test_presentation_follows_the_output_clock_and_health_needs_presented_audio() -> None:
    out = _node_json(_CLOCKS, str(LAB))
    # a positive, non-regressing contextTime is presentation; 0 is warm-up
    assert out["presented"] == [True, False, False]
    assert out["stalled"] is None  # a correlation alone never dates audio the output did not play
    assert out["reached"] == {"at": 5500, "method": "correlated"}  # the poll at 1.6 s maps 1.5 s exactly
    assert out["observed"]["method"] == "observed"
    assert out["health"][0] is None  # control: really playing is healthy
    assert "not audible" in out["health"][1] and "not presented" in out["health"][2]
    # loaded: a stopped deck holding the expected track is fine (control); another
    # track, a processor error on the right track, a pending transport and no deck are not
    assert out["loaded"][0] is None, out["loaded"]
    assert out["loaded"][1:] == [
        "holds b, expected a",
        "processor_error=decode failed",
        "transport pending",
        "no deck state",
    ]
    assert out["faults"][0] == [] and len(out["faults"][1]) == 2, out["faults"]


_HEAP = (
    _PRELUDE
    + """
const out_dir = process.argv[3];
const fs = await import('node:fs');
const cdp = await ctx.newCDPSession(page);
await cdp.send('HeapProfiler.enable');
const snap = async (n) => {
  await cdp.send('HeapProfiler.collectGarbage');
  const fd = fs.openSync(path.join(out_dir, `heap-${n}.heapsnapshot`), 'w');
  const onChunk = (e) => fs.writeSync(fd, e.chunk);
  cdp.on('HeapProfiler.addHeapSnapshotChunk', onChunk);
  await cdp.send('HeapProfiler.takeHeapSnapshot', { reportProgress: false });
  cdp.off('HeapProfiler.addHeapSnapshotChunk', onChunk);
  fs.closeSync(fd);
};
// each cycle leaks 2000 PerflabPlantedLeak and churns 2000 PerflabChurn that die
const cycle = () => page.evaluate(() => {
  class PerflabPlantedLeak { constructor(i) { this.i = i; } }
  class PerflabChurn { constructor(i) { this.i = i; } }
  window.__leak ??= [];
  window.__churn = [];
  for (let i = 0; i < 2000; i++) { window.__leak.push(new PerflabPlantedLeak(i)); window.__churn.push(new PerflabChurn(i)); }
});
await cycle();  // warm-up, as the driver's cycle 0: its leak predates A and is not a candidate
await snap('A');
for (let c = 0; c < 2; c++) await cycle();
await snap('B');
for (let c = 0; c < 2; c++) await cycle();
await snap('C');
console.log(JSON.stringify({ ok: true }));
await browser.close();
"""
)


def test_heapdiff_finds_a_planted_leak_in_real_snapshots_and_drops_churn(tmp_path: Path) -> None:
    """MemLab's three-snapshot method on real V8 snapshots: objects allocated
    between A and B and alive at C are exactly the planted leak's 4000, not
    the 2000 already alive at A; churn whose last batch survives only B -> C
    was allocated after B, so it is not a candidate (control: the method can
    say no)."""
    assert _node_json(_HEAP, str(LAB), _chromium(), str(tmp_path), timeout=180) == {"ok": True}
    r = subprocess.run(
        [sys.executable, str(LAB / "heapdiff.py"), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    leak = {x["name"]: x["count"] for x in json.loads((tmp_path / "heapdiff.json").read_text())["leak_candidates"]}
    assert leak.get("PerflabPlantedLeak") == 4000, leak
    assert "PerflabChurn" not in leak, leak


_PLANT = (
    _PRELUDE
    + """
const { plantBuffer, releasePlantedBuffer } = await import(path.join(lab, 'controls.mjs'));
const cdp = await ctx.newCDPSession(page);
const out = { nothing: await releasePlantedBuffer(page, cdp) };
await plantBuffer(page, 8);
out.planted = await page.evaluate(() => window.__perflabPlantedBuffer.byteLength);
out.released = await releasePlantedBuffer(page, cdp);
out.gone = await page.evaluate(() => !('__perflabPlantedBuffer' in window) && !('__perflabPlantedRef' in window));
// control: a second reference the release cannot see keeps it alive, and that is reported
await plantBuffer(page, 8);
await page.evaluate(() => { window.__other = window.__perflabPlantedBuffer; });
out.held = await releasePlantedBuffer(page, cdp);
await browser.close();
console.log(JSON.stringify(out));
"""
)


def test_the_planted_buffer_is_collected_before_the_measured_phases() -> None:
    """--controls plants a retained buffer for the before/after-plant dumps; it
    must then be gone, or every later dump and heap snapshot counts harness
    memory as the app's. The release proves collection through a WeakRef in a
    real V8, and says so when something still holds the buffer."""
    out = _node_json(_PLANT, str(LAB), _chromium(), timeout=120)
    assert out == {
        "nothing": "planted buffer nothing was planted",
        "planted": 8 * 1048576,
        "released": None,
        "gone": True,
        "held": "planted buffer still reachable after a forced collection",
    }, out


_RENDERED = (
    _PRELUDE
    + """
const { renderedControl } = await import(path.join(lab, 'frame-clock.mjs'));
await page.addScriptTag({ content: `window.__rc = ${renderedControl.toString()}` });
const html = (playing, fader, low) => `
  <button data-testid="play-deck-2" aria-pressed="${playing}"></button>
  <div role="slider" data-testid="channel-2-fader" aria-valuenow="${fader}"></div>
  <div role="slider" data-testid="knob-2:low" aria-valuenow="${low}"></div>`;
const read = () => page.evaluate(() => ({
  play: window.__rc('play', 2, null), pause: window.__rc('pause', 2, null),
  fader_cut: window.__rc('fader_cut', 2, 0), fader_open: window.__rc('fader_open', 2, 1),
  eq_low_kill: window.__rc('eq_low_kill', 2, 0), eq_low_restore: window.__rc('eq_low_restore', 2, 0.5),
  other_deck: window.__rc('play', 1, null), idle: window.__rc('idle_control', 2, null),
}));
await page.evaluate((h) => { document.body.innerHTML = h; }, html(true, 0, 0));
const killed = await read();
await page.evaluate((h) => { document.body.innerHTML = h; }, html(false, 1, 0.5));
const restored = await read();
await page.evaluate(() => { document.body.innerHTML = ''; });
const missing = await read();
await browser.close();
console.log(JSON.stringify({ killed, restored, missing }));
"""
)


def test_the_frame_is_timed_only_once_the_control_shows_the_change() -> None:
    """A query that reflects the command does not mean any control shows it:
    the latency frame waits for the page's own play button, channel fader or
    low EQ knob to render the commanded state, read from the attributes those
    components render (real Chromium DOM). A missing control never passes."""
    out = _node_json(_RENDERED, str(LAB), _chromium(), timeout=120)
    off = dict.fromkeys(("play", "pause", "fader_cut", "fader_open", "eq_low_kill", "eq_low_restore"), False)
    assert out["killed"] == {
        **off,
        "play": True,
        "fader_cut": True,
        "eq_low_kill": True,
        "other_deck": False,
        "idle": False,
    }
    assert out["restored"] == {
        **off,
        "pause": True,
        "fader_open": True,
        "eq_low_restore": True,
        "other_deck": False,
        "idle": False,
    }
    assert out["missing"] == {**off, "other_deck": False, "idle": False}


def test_rendered_control_reads_the_attributes_the_components_render() -> None:
    """The selectors renderedControl reads are the ones the Svelte controls
    emit, so renaming either side breaks this rather than leaving every frame
    unmeasured (or measuring the wrong element)."""
    rb = LAB.parents[2] / "apps/webui/frontend/src/lib"
    transport = (rb / "components/rb/deck/TransportCluster.svelte").read_text(encoding="utf-8")
    assert "data-testid={`play-deck-${deck.deck_id}`}" in transport and "aria-pressed={deck.playing}" in transport
    fader = (rb / "components/rb/mixer/VFader.svelte").read_text(encoding="utf-8")
    assert "data-testid={`channel-${deckId}-fader`}" in fader and "aria-valuenow={value}" in fader
    knob = (rb / "components/rb/mixer/Knob.svelte").read_text(encoding="utf-8")
    # displayValue, not value: since ae485ded82e the knob reports the live drag value.
    assert "data-testid={`knob-${knobId}`}" in knob and "aria-valuenow={displayValue}" in knob
    assert "return `${deckId}:${role}`;" in (rb / "rb/knob-control.svelte.ts").read_text(encoding="utf-8")
    strip = (rb / "components/rb/mixer/ChannelStrip.svelte").read_text(encoding="utf-8")
    # The LOW knob is wired directly since 7d0f49d25f1 removed the dialView remap.
    assert "knobId={knobId(deckId, 'low')}" in strip and "value={eqLow}" in strip


def test_budget_latency_runs_untraced_and_only_its_rows_are_budgeted() -> None:
    """The contract p99s come from the pass after the trace stops; the traced
    pass only feeds attribution and never reaches latencyRows."""
    src = (LAB / "driver.mjs").read_text(encoding="utf-8")
    main = src[src.index("await startTrace();") :]
    traced = main.index("file: 'latency-traced.json', phaseName: 'latency-traced', budget: false")
    stop = main.index("meta.trace_bytes = await stopTrace(")
    budget = main.index("file: 'latency.json', phaseName: 'latency', budget: true")
    assert traced < stop < budget
    assert "const rows = budget ? latencyRows : [];" in src


def test_a_processor_fault_in_latency_is_caught_before_a_reload_clears_it() -> None:
    """A load clears processor_error, and the leak cycles reload every deck, so
    each latency rep checks deck 1 itself and the leak checks the held decks
    before its first reload."""
    src = (LAB / "driver.mjs").read_text(encoding="utf-8")
    lat = src[src.index("async function scenarioLatency(") : src.index("async function scenarioLeak(")]
    check = "await processorFaults(`rep ${rep}`);"
    assert (
        lat.index("appModel.take(await perfRing(), action);")
        < lat.index(check)
        < lat.index("appModel.take(await perfRing());")
    )
    assert "meta.invalid ??= `${phaseName} ${when}: ${faults.join('; ')}`;" in lat
    leak = src[src.index("async function scenarioLeak(") :]
    before = leak.index("const before = await loadedDecksFaults(page, held);")
    assert before < leak.index("await cycle(0);")
    assert "meta.invalid ??= `before leak: ${before.join('; ')}`;" in leak


def test_node_floor_refuses_a_runtime_below_the_frontend_floor(tmp_path: Path) -> None:
    """node-floor.mjs applies node_runtime.py's rule to the scripts that run
    under node directly: below the >= floor is refused, at or above it is not."""
    pkg = tmp_path / "package.json"
    pkg.write_text(json.dumps({"engines": {"node": ">=22.14.0"}}), encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"engines": {"node": "^22"}}), encoding="utf-8")
    script = """
import path from 'node:path';
const [lab, pkg, bad] = process.argv.slice(1);
const { nodeFloorRefusal } = await import(path.join(lab, 'node-floor.mjs'));
const v = ['18.0.0', '22.13.9', '22.14.0', '22.14.1', '22.15.0', '23.0.0', '21.99.99'];
console.log(JSON.stringify({ ...Object.fromEntries(v.map((x) => [x, nodeFloorRefusal(x, pkg)])), bad: nodeFloorRefusal('22.14.0', bad) }));
"""
    out = _node_json(script, str(LAB), str(pkg), str(bad))
    for refused in ("18.0.0", "22.13.9", "21.99.99"):
        assert out[refused] == f"Node {refused} is below the frontend floor 22.14.0"
    for allowed in ("22.14.0", "22.14.1", "22.15.0", "23.0.0"):
        assert out[allowed] is None
    assert "not a >= floor" in out["bad"]


def test_driver_and_abwin_check_node_before_loading_playwright() -> None:
    """The full-profile driver is launched by hand with `node`, so it (and
    abwin) refuse an old runtime themselves, before Playwright or a measurement."""
    for name, load in (
        ("driver.mjs", "const { chromium } = requireFromFrontend('playwright');"),
        ("abwin.mjs", "const { chromium } = require('playwright');"),
    ):
        src = (LAB / name).read_text(encoding="utf-8")
        check = src.index("const nodeRefusal = nodeFloorRefusal(process.versions.node,")
        assert check < src.index(load), name
        assert "process.exit(2);" in src[check : src.index(load)], name


def test_a_press_refused_in_the_traced_pass_voids_the_capture() -> None:
    """The traced pass's rows reach no KPI or captureFaults, but its trace
    windows are attributed, so a press that failed there voids the capture."""
    src = (LAB / "driver.mjs").read_text(encoding="utf-8")
    lat = src[src.index("async function scenarioLatency(") : src.index("async function scenarioLeak(")]
    push = lat.index("rows.push(row);")
    guard = lat.index("if (!budget && row.press_error !== null) {")
    assert push < guard < lat.index("await processorFaults(`rep ${rep}`);")
    assert "meta.invalid ??= `${phaseName} rep ${rep} ${action}: press failed: ${row.press_error}`;" in lat


def test_an_empty_workload_is_refused_before_anything_runs(tmp_path: Path) -> None:
    """Real driver.mjs and abwin.mjs: a zero, negative, fractional or
    non-numeric count would run an empty workload that still writes a
    complete-looking capture (three identical leak snapshots read OK), so each
    is refused by name. Control: valid counts pass this check and reach the
    next one (steady needs four ids), so the refusal is not blanket."""

    def driver(*extra: str) -> str:
        argv = [_node(), str(LAB / "driver.mjs"), "--url", "http://127.0.0.1:9", "--ids", "a"]
        argv += ["--out", str(tmp_path / "run"), "--commit", "c", *extra]
        r = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
        assert r.returncode != 0, r.stderr
        return r.stderr

    for flag, value in (
        ("--leak-cycles", "0"),
        ("--leak-cycles", "-2"),
        ("--leak-cycles", "1.5"),
        ("--latency-reps", "0"),
        ("--viewport", "0x1080"),
    ):
        err = driver("--scenario", "leak", flag, value)
        assert "must be a positive integer, got" in err and flag in err, err
    for value in ("0", "-1", "soon"):
        assert "--phase-s must be a positive number of seconds" in driver("--scenario", "leak", "--phase-s", value)
    ok = driver("--scenario", "steady", "--leak-cycles", "1", "--latency-reps", "1", "--phase-s", "0.5")
    assert "positive" not in ok and "needs 4 --ids, got 1" in ok, ok
    ab = [_node(), str(LAB / "abwin.mjs"), "--a", "http://127.0.0.1:9", "--b", "http://127.0.0.1:9"]
    ab += ["--out", str(tmp_path / "ab"), "--states", "idle-empty"]
    for value in ("0", "-3", "x"):
        r = subprocess.run([*ab, "--win", value], capture_output=True, text=True, timeout=60, check=False)
        assert r.returncode != 0 and "--win must be a positive number of seconds" in r.stderr, r.stderr


_GRAPHICS = """
import path from 'node:path';
import { createRequire } from 'node:module';
const [lab, chrome] = process.argv.slice(1);
const { graphicsIdentity } = await import(path.join(lab, 'capture-meta.mjs'));
const { chromium } = createRequire(path.join(lab, '../../../apps/webui/frontend/package.json'))('playwright');
const browser = await chromium.launch({ headless: true, executablePath: chrome });
try {
  console.log(JSON.stringify({ first: await graphicsIdentity(browser), again: await graphicsIdentity(browser) }));
} finally {
  await browser.close();
}
"""


def test_graphics_identity_reads_the_gpu_from_a_real_browser() -> None:
    """Real Chromium: the identity names the GPU device, its driver and the GL
    renderer in use, keeps only stable fields (two reads agree), and so is
    what the ledger can require equal between two captures."""
    out = _node_json(_GRAPHICS, str(LAB), _chromium())
    g = out["first"]
    assert out["again"] == g
    assert g["devices"] and all(d["vendor"] and d["device"] and d["driver"] for d in g["devices"]), g
    assert g["gl_renderer"] and g["gl_vendor"] and g["gl_version"] and g["gl_implementation"], g
    assert set(g) == {"devices", "gl_renderer", "gl_vendor", "gl_version", "gl_implementation", "skia_backend"}
    src = (LAB / "driver.mjs").read_text(encoding="utf-8")
    assert src.index("const graphics = await graphicsIdentity(browser);") < src.index(
        "const context = await browser.newContext("
    )
    assert "meta.graphics = graphics;" in src


_BROWSER_ENV = """
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
const [lab, chrome] = process.argv.slice(1);
const { browserEnv } = await import(path.join(lab, 'browser-env.mjs'));
const { chromium } = createRequire(path.resolve(lab, '../../../apps/webui/frontend/package.json'))('playwright');
// the environment the launched browser process really holds, read back from the OS
const childEnv = () => {
  for (const pid of fs.readdirSync('/proc').filter((d) => /^\\d+$/.test(d))) {
    let status;
    try { status = fs.readFileSync(`/proc/${pid}/status`, 'utf8'); } catch { continue; }
    if (status.match(/^PPid:\\s+(\\d+)/m)?.[1] !== String(process.pid)) continue;
    const env = fs.readFileSync(`/proc/${pid}/environ`, 'utf8').split('\\0').filter(Boolean);
    if (fs.readlinkSync(`/proc/${pid}/exe`) === fs.realpathSync(chrome)) return env.map((e) => e.split('=')[0]);
  }
  return null;
};
process.env.MALLOC_ARENA_MAX = '1';
process.env.PERFLAB_UNKNOWN_KNOB = '1';
const out = {};
for (const [name, opts] of [['allowlisted', { env: browserEnv() }], ['inherited', {}]]) {
  const browser = await chromium.launch({ executablePath: chrome, ...opts });
  out[name] = childEnv();
  await browser.close();
}
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads the launched browser's environment from /proc")
def test_a_measured_browser_inherits_only_the_allowlisted_environment() -> None:
    """MALLOC_ARENA_MAX, LD_PRELOAD or a Chromium runtime variable in the
    driver's shell would change browser CPU and memory without reaching the
    manifest: the real Chromium launched with browserEnv() holds only
    allowlisted names, read back from the OS, while the same launch without it
    holds the driver's whole environment (control)."""
    out = _node_json(_BROWSER_ENV, str(LAB), _chromium())
    allowed = {"PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "TEMP", "TMP", "DISPLAY", "XAUTHORITY"}
    allowed |= {"WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"}
    assert out["allowlisted"] is not None and out["inherited"] is not None, out
    assert "PATH" in out["allowlisted"], out["allowlisted"]
    # Chromium may add its own variables to its process; none of the driver's may pass
    assert "MALLOC_ARENA_MAX" not in out["allowlisted"] and "PERFLAB_UNKNOWN_KNOB" not in out["allowlisted"]
    assert {"MALLOC_ARENA_MAX", "PERFLAB_UNKNOWN_KNOB"} <= set(out["inherited"])  # control
    leaked = set(out["allowlisted"]) & (set(out["inherited"]) - allowed)
    added = set(out["allowlisted"]) - set(out["inherited"])
    assert leaked <= added, leaked


def test_every_measured_browser_launch_takes_the_allowlisted_environment() -> None:
    """Each chromium.launch in the measuring scripts passes browserEnv(), so no
    launch path inherits the shell (the probe above proves what that holds)."""
    for name in ("driver.mjs", "abwin.mjs"):
        src = (LAB / name).read_text(encoding="utf-8")
        launches = src.count("chromium.launch(")
        assert launches and launches == src.count("chromium.launch({ env: browserEnv(),"), name
