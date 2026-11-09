// requirement: FXROLL-10595
// [if] the slip return gains do not sum to 1 or are not linear [then] broken
// [if] clickRatio does not flag a hard step or does not read 1 on an unbroken sine [then] broken
// [if] the hidden head at release is more than 1 ms off the anchor algebra [then] broken
// [if] the hygiene check misses a sample over 0 dBFS, a NaN or a subnormal [then] broken
// [if] arm() does not ramp at the switch time or does not restore processor -> analyser [then] broken
// [if] arm() blends over a stem processor or one that crossfades its own seeks [then] broken
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const blend = await loadTypeScriptModule('src/lib/rb/slip-return-blend.ts');
const slip = await loadTypeScriptModule('src/lib/player/transport/slip-anchor.ts');

const SR = 48000;

// The click veto, the length choice, the null test and window hygiene are measured on
// real decoded audio through real graphs in tests/e2e/slip-return-blend-real-audio.spec.ts.

function hiddenHeadFrame(engageFrame, releaseFrame, boundaries = []) {
	const anchor = slip.createSlipAnchor({
		startContextTime: engageFrame / SR,
		startPositionSec: engageFrame / SR,
		tempoRatio: 1,
		durationSec: 600
	});
	return slip.slipHiddenPositionWithTempoBoundaries(anchor, boundaries, releaseFrame / SR) * SR;
}

function sine(length, stepAt = -1) {
	const x = new Float32Array(length);
	for (let n = 0; n < length; n++) {
		x[n] = 0.5 * Math.sin((2 * Math.PI * 440 * n) / SR) + (stepAt >= 0 && n >= stepAt ? 0.5 : 0);
	}
	return x;
}

//----- tests

test('slipReturnBlendGain is linear and sums to 1', () => {
	const frames = 256;
	let prev = -1;
	for (let f = 0; f <= frames; f++) {
		const g = blend.slipReturnBlendGain(f, frames);
		assert.equal(g.old + g.incoming, 1, 'if slip return gains do not sum to 1 then broken');
		assert.ok(g.incoming > prev, 'if slip return incoming gain is not monotonic then broken');
		prev = g.incoming;
	}
	assert.equal(blend.slipReturnBlendGain(0, frames).incoming, 0);
	assert.equal(blend.slipReturnBlendGain(frames, frames).incoming, 1);
	assert.equal(blend.slipReturnBlendGain(64, 256).incoming, 0.25);
	assert.throws(() => blend.slipReturnBlendGain(-1, frames), RangeError);
	assert.throws(() => blend.slipReturnBlendGain(Number.NaN, frames), RangeError);
	assert.throws(() => blend.slipReturnBlendGain(0, 0), RangeError);
});

test('clickRatio flags a step, passes an unbroken sine and rejects a silent source', () => {
	const event = 2048;
	const clean = sine(SR);
	const stepped = sine(SR, event);
	assert.ok(
		blend.clickRatio(stepped, [clean], event) > blend.SLIP_RETURN_CLICK_VETO,
		'if clickRatio does not flag a hard step then broken'
	);
	assert.ok(
		Math.abs(blend.clickRatio(clean, [clean], event) - 1) < 1e-9,
		'if clickRatio of an unbroken sine against itself is not 1 then broken'
	);
	assert.throws(() => blend.clickRatio(clean, [new Float32Array(SR)], event), RangeError);
});

test('hidden head at release matches the anchor algebra within 1 ms', () => {
	const engage = SR * 2;
	const release = SR * 6;
	assert.ok(Math.abs(hiddenHeadFrame(engage, release) / SR - 6) * 1000 <= 1);
	// tempo boundary at 1.02 halfway through the roll
	const mid = SR * 4;
	const head = hiddenHeadFrame(engage, release, [{ startContextTime: mid / SR, tempoRatio: 1.02 }]);
	const analytic = 2 + 2 * 1 + 2 * 1.02;
	assert.ok(
		Math.abs(head / SR - analytic) * 1000 <= 1,
		'if position_ms at release is off the hidden head by > 1 ms then broken'
	);
});

test('the window hygiene check catches bad samples', () => {
	for (const bad of [1.2, Number.NaN, 1e-40]) {
		const x = new Float32Array(8);
		x[3] = bad;
		assert.throws(() => blend.assertBlendWindowClean(x, 0, 8), RangeError);
	}
	blend.assertBlendWindowClean(new Float32Array(8), 0, 8);
});

//----- graph glue

class AutoParam {
	constructor() {
		this.value = 0;
		this.events = [];
	}
	setValueAtTime(v, t) {
		this.events.push(['set', v, t]);
	}
	linearRampToValueAtTime(v, t) {
		this.events.push(['ramp', v, t]);
	}
}

function fakeGraph({ startThrows = false } = {}) {
	const log = [];
	const node = (name) => ({
		name,
		connect: (to) => log.push(`${name}->${to.name}`),
		disconnect: () => log.push(`${name} x`)
	});
	const sources = [];
	const ctx = {
		currentTime: 1,
		sampleRate: SR,
		createGain() {
			const n = node(`gain${log.filter((l) => l.startsWith('new gain')).length}`);
			log.push(`new ${n.name}`);
			n.gain = new AutoParam();
			return n;
		},
		createBufferSource() {
			const n = node('src');
			n.playbackRate = new AutoParam();
			n.start = (when, offset) => {
				if (startThrows) throw new Error('start failed');
				n.started = [when, offset];
			};
			n.stop = () => log.push('src stop');
			sources.push(n);
			return n;
		}
	};
	const analyser = node('analyser');
	const processor = node('processor');
	const runtime = { audioBuffer: { duration: 300 }, nodes: { analyser }, processor, loadToken: 1 };
	return { ctx, runtime, log, sources };
}

function withTimers(fn) {
	const original = globalThis.setTimeout;
	const pending = [];
	globalThis.setTimeout = (cb, ms) => pending.push([cb, ms]);
	try {
		fn(pending);
	} finally {
		globalThis.setTimeout = original;
	}
}

test('arm ramps at the switch time and restores processor -> analyser', () => {
	withTimers((pending) => {
		const g = fakeGraph();
		const handle = blend.prepareSlipReturnBlend({
			context: g.ctx,
			runtime: g.runtime,
			isStemProcessor: false,
			oldPositionSecAt: (t) => t * 2,
			loop: { startSec: 10, endSec: 10.5 },
			tempoRatio: 1.01
		});
		const T = 1.2;
		assert.equal(handle.arm(T), 'blended');
		assert.equal(g.log.filter((l) => l.startsWith('new gain')).length, 2);
		const src = g.sources[0];
		assert.deepEqual(src.started, [T, 2.4]);
		assert.equal(src.loop, true);
		assert.equal(src.loopStart, 10);
		assert.equal(src.loopEnd, 10.5);
		assert.equal(src.playbackRate.value, 1.01);
		assert.equal(pending.length, 1);
		pending[0][0]();
		assert.equal(
			g.log[g.log.length - 1],
			'processor->analyser',
			'if the cleanup does not reconnect processor -> analyser then broken'
		);
	});
});

test('arm ramp automation lands on T and T + frames / sr', () => {
	withTimers(() => {
		const g = fakeGraph();
		const gains = [];
		const create = g.ctx.createGain.bind(g.ctx);
		g.ctx.createGain = () => {
			const n = create();
			gains.push(n);
			return n;
		};
		const frames = blend.SLIP_RETURN_BLEND_FRAMES;
		blend
			.prepareSlipReturnBlend({
				context: g.ctx,
				runtime: g.runtime,
				isStemProcessor: false,
				processorCrossfadesSeeks: false,
				oldPositionSecAt: () => 3,
				loop: null,
				tempoRatio: 1
			})
			.arm(2);
		const end = 2 + frames / SR;
		assert.deepEqual(gains[0].gain.events, [
			['set', 1, 1],
			['set', 0, 2],
			['ramp', 1, end]
		]);
		assert.deepEqual(gains[1].gain.events, [
			['set', 0, 1],
			['set', 1, 2],
			['ramp', 0, end]
		]);
	});
});

test('arm skips stem processors, self-crossfading processors and past times without touching the graph', () => {
	const g = fakeGraph();
	const deps = {
		context: g.ctx,
		runtime: g.runtime,
		oldPositionSecAt: () => 0,
		loop: null,
		tempoRatio: 1
	};
	assert.equal(
		blend
			.prepareSlipReturnBlend({ ...deps, isStemProcessor: true, processorCrossfadesSeeks: true })
			.arm(2),
		'hard:stem-processor'
	);
	assert.equal(
		blend
			.prepareSlipReturnBlend({ ...deps, isStemProcessor: false, processorCrossfadesSeeks: true })
			.arm(2),
		'hard:processor-crossfades',
		'if arm() blends over a processor that crossfades its own seeks then broken'
	);
	assert.equal(
		blend
			.prepareSlipReturnBlend({ ...deps, isStemProcessor: false, processorCrossfadesSeeks: false })
			.arm(0.5),
		'hard:past'
	);
	assert.deepEqual(g.log, []);
});

test('a throwing outSrc.start rethrows and leaves processor -> analyser connected', () => {
	withTimers((pending) => {
		const g = fakeGraph({ startThrows: true });
		const handle = blend.prepareSlipReturnBlend({
			context: g.ctx,
			runtime: g.runtime,
			isStemProcessor: false,
			oldPositionSecAt: () => 0,
			loop: null,
			tempoRatio: 1
		});
		assert.throws(() => handle.arm(2), /start failed/);
		assert.equal(g.log[g.log.length - 1], 'processor->analyser');
		assert.equal(pending.length, 0);
	});
});
