import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// ANIM-CLOCK-01 regression lines (each describe carries a control that proves
// its assertion bites):
// - if the display projects from the frame that noticed a sample rather than the sample's output time then broken
// - if a drift under slewMaxErrorMs makes the display jump at the sample instant then broken
// - if a drift over snapErrorMs, a track change, a resume or a tempo step is slewed instead of snapped then broken
// - if a paused or stalled deck is projected past its last confirmed sample then broken
// - if chunky output timestamps at 60 fps paint with a p95 frame deviation over 1 ms, or snap in steady play, then broken
//
// Why: the maintainer, Mon 5 Oct 2026, "animations (waveform + rotating beat thingies
// etc) should probably be smoothed via assumption with jump-catch up if it
// desyncs to prevent these jumpy things happening".

let clock;
let display;
const CFG = () => clock.PLAYHEAD_CLOCK_CONFIG;

before(async () => {
	clock = await loadTypeScriptModule('src/lib/rb/playhead-clock.ts');
	display = await loadTypeScriptModule('src/lib/rb/playhead-display.svelte.ts');
});

function sample(positionMs, sampledAtMs, extra = {}) {
	return { positionMs, sampledAtMs, rate: 1, playing: true, trusted: true, trackKey: 'a', ...extra };
}

/** Deterministic frame jitter (xorshift), so a failure reproduces exactly. */
function rng(seed) {
	let x = seed;
	return () => {
		x ^= x << 13;
		x ^= x >>> 17;
		x ^= x << 5;
		return ((x >>> 0) % 10000) / 10000;
	};
}

function p95(values) {
	const sorted = [...values].sort((a, b) => a - b);
	return sorted[Math.floor(0.95 * sorted.length)];
}

/**
 * One deck playing at `rate` through an output device that hands over its
 * clock in `bufferMs` chunks with `latencyMs` of output latency, painted by
 * rAF at ~60 fps with up to `jitterMs` of frame-time jitter. Returns the
 * per-frame deviation |painted delta - rate x frame interval| for the clock's
 * display and for the raw sample, plus every reconcile verdict.
 */
function simulate({ seconds = 60, rate = 1.016, bufferMs = 10.667, latencyMs = 25, jitterMs = 4, stampJitterMs = 1, anchorAtNotice = false } = {}) {
	const random = rng(7);
	const state = clock.initPlayheadClock();
	const verdicts = [];
	const shown = [];
	const raw = [];
	const frames = [];
	let t = 1000;
	while (t < 1000 + seconds * 1000) {
		t += 16.667 + (random() - 0.5) * jitterMs;
		const contextMs = Math.floor((t - latencyMs) / bufferMs) * bufferMs;
		const positionMs = 30000 + contextMs * rate;
		const sampledAt = anchorAtNotice ? t : contextMs + latencyMs + (random() - 0.5) * stampJitterMs;
		verdicts.push(clock.notePlayheadSample(state, sample(positionMs, sampledAt, { rate }), t));
		shown.push(clock.playheadDisplayMs(state, t));
		raw.push(positionMs);
		frames.push(t);
	}
	const deviations = (series) => series.slice(1).map((v, i) => Math.abs(v - series[i] - rate * (frames[i + 1] - frames[i])));
	return { shown: deviations(shown), raw: deviations(raw), verdicts };
}

describe('dead reckoning from the output timestamp', () => {
	it('projects from the sample time at the sample rate, not from when it was noticed', () => {
		const state = clock.initPlayheadClock();
		clock.notePlayheadSample(state, sample(5000, 1000, { rate: 1.08 }), 1150);
		assert.ok(Math.abs(clock.playheadDisplayMs(state, 1150) - (5000 + 150 * 1.08)) < 1e-9);
		assert.ok(Math.abs(clock.playheadDisplayMs(state, 1160) - (5000 + 160 * 1.08)) < 1e-9);
	});

	it('two elements reading one tick in different frames paint synced decks in phase (BEATSYNC-PAINT-01)', () => {
		const a = clock.initPlayheadClock();
		const b = clock.initPlayheadClock();
		clock.notePlayheadSample(a, sample(5000, 1000), 1000);
		clock.notePlayheadSample(b, sample(7000, 1000), 1150); // noticed a whole busy frame later
		assert.equal(clock.playheadDisplayMs(b, 1150) - clock.playheadDisplayMs(a, 1150), 2000, 'if decks the engine holds 2000 ms apart paint apart by anything else then they slip - broken');
	});

	it('control: anchoring at notice time paints the same tick 150 ms out of phase', () => {
		const a = clock.initPlayheadClock();
		const b = clock.initPlayheadClock();
		clock.notePlayheadSample(a, sample(5000, 1000), 1000);
		clock.notePlayheadSample(b, sample(7000, 1150), 1150);
		assert.equal(clock.playheadDisplayMs(b, 1150) - clock.playheadDisplayMs(a, 1150), 1850);
	});

	it('takes performanceTime only while the output clock drives presentation', () => {
		assert.equal(display.presentedSampleAtMs({ clock_source: 'output' }, { performanceTime: 990 }, 1000), 990);
		assert.equal(display.presentedSampleAtMs({ clock_source: 'sample' }, { performanceTime: 990 }, 1000), 1000, 'if the sample-clock fallback is stamped with a stale output time then it projects from the wrong instant - broken');
		assert.equal(display.presentedSampleAtMs({ clock_source: 'output' }, { performanceTime: Number.NaN }, 1000), 1000);
		assert.equal(display.presentedSampleAtMs({ clock_source: 'output' }, { performanceTime: -1 }, 1000), 1000);
	});
});

describe('reconciling drift', () => {
	it('slews a small drift out: no jump at the sample instant, gone after the slew window', () => {
		const state = clock.initPlayheadClock();
		clock.notePlayheadSample(state, sample(5000, 1000), 1000);
		const before = clock.playheadDisplayMs(state, 1100); // 5100
		assert.equal(clock.notePlayheadSample(state, sample(5090, 1100), 1100), 'slew'); // truth is 10 ms behind
		assert.ok(Math.abs(clock.playheadDisplayMs(state, 1100) - before) < 1e-9, 'if a 10 ms drift moves the playhead at the sample instant then it jumps - broken');
		assert.ok(Math.abs(clock.playheadDisplayMs(state, 1100 + CFG().slewWindowMs) - (5090 + CFG().slewWindowMs)) < 1e-9, 'if the drift survives the slew window then it accumulates - broken');
		const step = clock.playheadDisplayMs(state, 1117) - clock.playheadDisplayMs(state, 1100);
		assert.ok(Math.abs(step - 17) <= (10 / CFG().slewWindowMs) * 17 + 1e-9, 'a slewing frame strays by at most drift x frame / window');
	});

	it('catches a mid-size drift up faster than a small one, still without a jump', () => {
		const state = clock.initPlayheadClock();
		clock.notePlayheadSample(state, sample(5000, 1000), 1000);
		const before = clock.playheadDisplayMs(state, 1100);
		assert.equal(clock.notePlayheadSample(state, sample(5070, 1100), 1100), 'catch-up'); // 30 ms
		assert.ok(Math.abs(clock.playheadDisplayMs(state, 1100) - before) < 1e-9);
		assert.ok(Math.abs(clock.playheadDisplayMs(state, 1100 + CFG().catchUpWindowMs) - (5070 + CFG().catchUpWindowMs)) < 1e-9);
	});

	it('snaps a drift over snapErrorMs at once', () => {
		const state = clock.initPlayheadClock();
		clock.notePlayheadSample(state, sample(5000, 1000), 1000);
		assert.equal(clock.notePlayheadSample(state, sample(9000, 1100), 1100), 'snap-error'); // a seek
		assert.equal(clock.playheadDisplayMs(state, 1100), 9000, 'if a seek is slewed then the playhead crawls to it - broken');
	});

	it('snaps real discontinuities even when the error is small: track change, resume, tempo step', () => {
		const load = clock.initPlayheadClock();
		clock.notePlayheadSample(load, sample(5000, 1000), 1000);
		assert.equal(clock.notePlayheadSample(load, sample(5095, 1100, { trackKey: 'b' }), 1100), 'snap-discontinuity');
		const resume = clock.initPlayheadClock();
		clock.notePlayheadSample(resume, sample(5000, 1000, { playing: false }), 1000);
		assert.equal(clock.notePlayheadSample(resume, sample(5000, 1100), 1100), 'snap-discontinuity');
		const tempo = clock.initPlayheadClock();
		clock.notePlayheadSample(tempo, sample(5000, 1000), 1000);
		assert.equal(clock.notePlayheadSample(tempo, sample(5100, 1100, { rate: 1.08 }), 1100), 'snap-discontinuity');
		assert.ok(Math.abs(clock.playheadDisplayMs(tempo, 1200) - (5100 + 100 * 1.08)) < 1e-9, 'a tempo change re-anchors: no error accumulated from the old rate');
	});

	it('control: a sync nudge under tempoStepRatio re-anchors and slews instead of snapping', () => {
		const state = clock.initPlayheadClock();
		clock.notePlayheadSample(state, sample(5000, 1000), 1000);
		assert.equal(clock.notePlayheadSample(state, sample(5100, 1100, { rate: 1.002 }), 1100), 'slew');
	});
});

describe('stall and pause freeze at the last confirmed position', () => {
	it('never projects a paused or untrusted sample, however much time passes', () => {
		for (const extra of [{ playing: false }, { trusted: false }]) {
			const state = clock.initPlayheadClock();
			clock.notePlayheadSample(state, sample(5000, 1000), 1000);
			assert.equal(clock.notePlayheadSample(state, sample(5100, 1100, extra), 1100), 'frozen');
			assert.equal(clock.playheadDisplayMs(state, 5000), 5100, `if a ${JSON.stringify(extra)} deck keeps moving then a stall is hidden - broken`);
		}
	});

	it('stops projecting maxProjectionMs past the last sample when samples stop arriving', () => {
		const state = clock.initPlayheadClock();
		clock.notePlayheadSample(state, sample(5000, 1000), 1000);
		const cap = CFG().maxProjectionMs;
		assert.equal(clock.playheadDisplayMs(state, 1000 + cap), 5000 + cap);
		assert.equal(clock.playheadDisplayMs(state, 1000 + cap + 500), 5000 + cap, 'if a starved clock runs on then the display leaves the audio behind - broken');
		assert.equal(clock.projectionCapped(state.anchor, 1000 + cap + 1), true);
	});

	it('control: a playing, trusted sample does advance over the same interval', () => {
		const state = clock.initPlayheadClock();
		clock.notePlayheadSample(state, sample(5000, 1000), 1000);
		assert.equal(clock.playheadDisplayMs(state, 1100), 5100);
		assert.equal(clock.projectionCapped(state.anchor, 1100), false);
	});
});

describe('steady play through a chunky output clock (the acceptance shape)', () => {
	it('paints a 60 s synced deck with a p95 frame deviation under 1 ms and no snaps', () => {
		const run = simulate();
		const snaps = run.verdicts.filter((v) => v.startsWith('snap')).length;
		assert.equal(snaps, 0, 'if steady play snaps then the playhead visibly jumps - broken');
		assert.ok(p95(run.shown) < 1, `p95 ${p95(run.shown)} ms: if the display strays from tempo x frame then it jitters - broken`);
	});

	it('control: the raw sample through the same clock strays by several ms per frame', () => {
		const run = simulate();
		assert.ok(p95(run.raw) > 3, `raw p95 ${p95(run.raw)} ms; if this is small the simulation is not chunky and proves nothing`);
	});

	it('control: anchoring each sample at the frame that noticed it fails the same 1 ms bar', () => {
		const run = simulate({ anchorAtNotice: true });
		assert.ok(p95(run.shown) > 1, `notice-anchored p95 ${p95(run.shown)} ms`);
	});
});
