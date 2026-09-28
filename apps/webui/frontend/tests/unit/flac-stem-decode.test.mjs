import assert from 'node:assert/strict';
import { before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Q18 rung 1: the four-way stem decode moves into four Web Workers.
 *
 * The defect this rung fixes is not a bug, it is a ceiling: WebKit decodes on
 * ONE thread, so `Promise.all` over four `decodeAudioData` calls buys 1.0x to
 * 1.3x and the operator waits ~1.4-1.9s for a decode that four workers finish
 * in ~0.7-1.1s (docs/perf/research/stems-decode-rung-bench.md).
 *
 * The dangerous part is not the speed-up, it is the three refusals. A WASM
 * decoder does NOT resample and `decodeAudioData` does, so a 48kHz bundle in a
 * 44.1kHz context comes back right in samples and wrong in seconds. The four
 * stems agree with EACH OTHER, so `validateStemBufferAlignment` passes them;
 * the engine's stem/source check then fails the load outright. Refusing here
 * is what keeps a bundle that used to load from stopping.
 *
 * Regression lines:
 * - if a non-FLAC part is handed to the FLAC decoder then a v3 bundle in any
 *   other codec fails a deck load that is actually fine
 * - if a decode at the wrong sample rate is accepted then the stems are right
 *   in samples and wrong in seconds, and alignment validation says nothing
 * - if a missing or broken decoder is fatal then a stemmed deck stops loading
 *   rather than loading at yesterday's speed
 * - if the parts are decoded sequentially then the workers never overlap and
 *   the whole rung buys nothing
 * - if a worker is not freed then a thread and a wasm heap leak per stem, per
 *   load
 * - if a refusal is silent then a load that lost its 1.7x looks like a slow
 *   machine instead of saying so
 */

let decode;

before(async () => {
	decode = await loadTypeScriptModule('tests/live/stem-decode-harness-entry.ts');
});

// The pool is module state by design (it holds real workers across loads), so
// a case that inherited a previous case's parked decoders would be testing the
// previous case.
beforeEach(() => {
	decode.stemDecodeSession.resetPool();
	decode.stemDecodeSession.resetLane();
	// Most cases are about the refusals, not about lane choice, so they run
	// with the workers lane already settled. The calibration cases below
	// deliberately do not call this.
	decode.stemDecodeSession.forceLane('workers');
});

const PARTS = ['vocals', 'drums', 'bass', 'other'];

function bytesWithHeader(header, length = 64) {
	const out = new Uint8Array(length);
	out.set(header, 0);
	return out.buffer;
}

const FLAC = () => bytesWithHeader([0x66, 0x4c, 0x61, 0x43]);

/**
 * FLAC bytes whose STREAMINFO DISCLOSES a rate.
 *
 * `FLAC()` above leaves the rate field zero, which the format reads as
 * unknown, so it exercises the path where only the decoder can say. This one
 * exercises the path where the bytes say up front.
 */
function flacAt(rate) {
	const out = new Uint8Array(64);
	out.set([0x66, 0x4c, 0x61, 0x43], 0);
	out[7] = 34;
	out[18] = (rate >> 12) & 0xff;
	out[19] = (rate >> 4) & 0xff;
	out[20] = ((rate << 4) & 0xf0) | 0x01;
	return out.buffer;
}
const allAt = (rate) => Object.fromEntries(PARTS.map((part) => [part, flacAt(rate)]));
const OGG = () => bytesWithHeader([0x4f, 0x67, 0x67, 0x53]);

/** An AudioContext stand-in: only the two members the module actually uses. */
function fakeContext(sampleRate = 44100) {
	return {
		sampleRate,
		created: [],
		createBuffer(channels, frames, rate) {
			const buffer = {
				numberOfChannels: channels,
				length: frames,
				sampleRate: rate,
				channels: Array.from({ length: channels }, () => null),
				copyToChannel(source, channel) {
					assert.ok(source.length <= frames, 'copyToChannel source must fit the buffer');
					buffer.channels[channel] = source;
				}
			};
			this.created.push(buffer);
			return buffer;
		}
	};
}

/** A decoder whose lifecycle and concurrency are both observable. */
function fakeDecoderFactory({ sampleRate = 44100, samples = 128, fail = false, concurrent = PARTS.length } = {}) {
	const state = { made: 0, freed: 0, resets: 0, live: 0, peakLive: 0, releases: [] };
	const factory = () => {
		state.made += 1;
		return {
			ready: Promise.resolve(),
			async decodeFile() {
				state.live += 1;
				state.peakLive = Math.max(state.peakLive, state.live);
				// Hold the slot open until every sibling has entered, so a
				// sequential implementation cannot fake a peak of 4.
				await new Promise((resolve) => {
					state.releases.push(resolve);
					if (state.releases.length === concurrent) {
						for (const release of state.releases) release();
					}
				});
				state.live -= 1;
				if (fail) throw new Error('synthetic decode failure');
				return {
					channelData: [new Float32Array(samples), new Float32Array(samples)],
					samplesDecoded: samples,
					sampleRate
				};
			},
			async reset() {
				state.resets += 1;
			},
			free() {
				state.freed += 1;
			}
		};
	};
	return { factory, state };
}

/**
 * A clock where the whole load takes exactly `wholeLoadMs`.
 *
 * Real elapsed time in a unit test is noise, and the lane choice compares two
 * elapsed times - so the comparison would be testing the machine.
 * decodeStemParts reads the clock at load start, around each part, and at load
 * end; only the first and last reads decide the trial, so the clock stays at 0
 * until its final read.
 */
function stepClock(wholeLoadMs) {
	// 1 load-start + 2 per part + 1 load-end.
	const total = 1 + 2 * PARTS.length + 1;
	let calls = 0;
	return () => {
		calls += 1;
		return calls >= total ? wholeLoadMs : 0;
	};
}

/**
 * A clock that gives the main-thread part and the worker parts fixed costs.
 *
 * Real elapsed time in a unit test is noise, and the lane choice is a
 * comparison of two elapsed times - so the comparison would be testing the
 * machine rather than the rule.
 */
function laneClock({ mainThreadMs, workerWallMs }) {
	let calls = 0;
	// decodeStemParts reads the clock once at part start and once at part end,
	// interleaved across the four parts by Promise.all ordering.
	const perPart = [mainThreadMs, workerWallMs, workerWallMs, workerWallMs];
	const starts = [0, 0, 0, 0];
	return () => {
		const i = calls % 4;
        const isStart = calls < 4;
		calls += 1;
		return isStart ? starts[i] : perPart[i];
	};
}

function allFlac() {
	return Object.fromEntries(PARTS.map((part) => [part, FLAC()]));
}

function countingFallback() {
	const calls = [];
	return {
		calls,
		fn: async (bytes) => {
			calls.push(bytes.byteLength);
			return { fallback: true, numberOfChannels: 2, length: 99, sampleRate: 44100 };
		}
	};
}

//-----------------------------------------------------------------------------

test('the container is sniffed from the bytes, not assumed', () => {
	assert.equal(decode.isFlacContainer(FLAC()), true);
	assert.equal(decode.isFlacContainer(OGG()), false);
	// A truncated read must not match on a prefix it never saw.
	assert.equal(decode.isFlacContainer(new Uint8Array([0x66, 0x4c]).buffer), false);
	assert.equal(decode.isFlacContainer(new ArrayBuffer(0)), false);
});

// REQ: PERF-STEMDEC-01
test('four FLAC parts decode in four workers, concurrently, and every worker is parked', async () => {
	const ctx = fakeContext();
	const { factory, state } = fakeDecoderFactory();
	const fallback = countingFallback();

	const result = await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: factory,
		decodeFallback: fallback.fn
	});

	assert.equal(state.made, 4, 'one decoder per stem');
	assert.equal(state.peakLive, 4, 'all four decodes must be in flight at once');
	assert.equal(state.freed, 0, 'a healthy decoder is parked for the next load, not destroyed');
	assert.equal(decode.stemDecodeSession.pooled(), 4, 'and all four are available to reuse');
	assert.deepEqual(fallback.calls, [], 'the main-thread decoder must not be touched');
	assert.deepEqual(Object.keys(result.buffers).sort(), [...PARTS].sort());
	assert.deepEqual(
		result.reports.map((r) => [r.part, r.viaWorker, r.refusal]),
		PARTS.map((part) => [part, true, null]),
		'reports stay in part order and record the path taken'
	);
	assert.ok(
		result.reports.every((r) => r.bytes > 0),
		'each report carries its encoded size, so two lanes stay comparable'
	);
	assert.deepEqual(decode.stemDecodeLabels(result.reports), {
		stem_decode: 'workers',
		stem_decode_workers: '4/4',
		stem_decode_lane: 'workers'
	});
});

// REQ: PERF-STEMDEC-01
test('a non-FLAC bundle falls back rather than failing the load', async () => {
	const ctx = fakeContext();
	const { factory, state } = fakeDecoderFactory();
	const fallback = countingFallback();
	const encoded = Object.fromEntries(PARTS.map((part) => [part, OGG()]));

	const result = await decode.decodeStemParts(ctx, encoded, PARTS, {
		makeDecoder: factory,
		decodeFallback: fallback.fn
	});

	assert.equal(state.made, 0, 'a FLAC-only decoder must never see non-FLAC bytes');
	assert.equal(fallback.calls.length, 4);
	assert.equal(Object.keys(result.buffers).length, 4, 'the deck still loads');
	assert.ok(result.reports.every((r) => r.refusal === 'not-flac' && !r.viaWorker));
	assert.equal(decode.stemDecodeLabels(result.reports).stem_decode, 'main-thread');
	assert.equal(decode.stemDecodeLabels(result.reports).stem_decode_refused, 'not-flac');
});

// REQ: PERF-STEMDEC-01
test('a decode at the wrong sample rate is refused, not resampled by hope', async () => {
	// THE trap: decodeAudioData resamples to the context rate and a WASM
	// decoder does not. Four 48kHz stems in a 44.1kHz context agree with each
	// other perfectly, so validateStemBufferAlignment passes them, and the
	// engine's stem/source comparison then fails a load that used to work.
	const ctx = fakeContext(44100);
	const { factory, state } = fakeDecoderFactory({ sampleRate: 48000 });
	const fallback = countingFallback();

	const result = await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: factory,
		decodeFallback: fallback.fn
	});

	assert.equal(fallback.calls.length, 4, 'every mismatched part must go the resampling path');
	assert.ok(result.reports.every((r) => r.refusal === 'sample-rate-mismatch'));
	assert.equal(ctx.created.length, 0, 'and no buffer at the wrong rate may be built at all');
	// CONTROL ON THE OVERSHOOT: only a decoder the DECODER complained about is
	// destroyed. This one is healthy - the bundle's rate is what was wrong - so
	// discarding it would respawn four workers and a wasm heap on every 48kHz
	// load, which no report would ever mention.
	assert.equal(state.freed, 0, 'a decoder refused for the BUNDLE stays healthy');
	assert.equal(decode.stemDecodeSession.pooled(), 4, 'and is parked for the next load');

	// POSITIVE CONTROL: the identical setup at the matching rate takes the
	// worker path, so the refusal above is the rate and not the harness.
	// The pool is cleared first because a refused-for-rate decoder is still
	// HEALTHY and gets parked - the control must exercise its own factory,
	// not inherit four decoders wired to the previous one's release barrier.
	// forceLane, not just resetPool: a rate mismatch now SETTLES the session on
	// the main thread (a rate the header did not disclose costs a discarded
	// worker decode, and repeating that every load is the defect this settling
	// closes), so without it the control would run the main-thread lane.
	decode.stemDecodeSession.resetPool();
	decode.stemDecodeSession.forceLane('workers');
	const matching = await decode.decodeStemParts(fakeContext(48000), allFlac(), PARTS, {
		makeDecoder: fakeDecoderFactory({ sampleRate: 48000 }).factory,
		decodeFallback: countingFallback().fn
	});
	assert.ok(matching.reports.every((r) => r.viaWorker));
});

test('a decoder that throws degrades to yesterday speed, and still frees', async () => {
	const ctx = fakeContext();
	const { factory, state } = fakeDecoderFactory({ fail: true });
	const fallback = countingFallback();

	const result = await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: factory,
		decodeFallback: fallback.fn
	});

	assert.equal(fallback.calls.length, 4, 'a broken decoder must not fail the deck load');
	assert.equal(state.freed, 4, 'a decoder that threw is destroyed, never reused');
	assert.equal(decode.stemDecodeSession.pooled(), 0, 'and must not be parked for the next load');
	assert.ok(result.reports.every((r) => r.refusal === 'decode-failed'));
});

test('an empty decode is refused instead of becoming a zero-length stem', async () => {
	const ctx = fakeContext();
	const fallback = countingFallback();
	const result = await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: fakeDecoderFactory({ samples: 0 }).factory,
		decodeFallback: fallback.fn
	});
	assert.equal(fallback.calls.length, 4);
	assert.ok(result.reports.every((r) => r.refusal === 'empty-decode'));
});

test('a mixed bundle reports mixed, so a half-won load cannot read as a whole one', () => {
	const labels = decode.stemDecodeLabels([
		{ part: 'vocals', viaWorker: true, refusal: null, ms: 10, codec: 'flac' },
		{ part: 'drums', viaWorker: false, refusal: 'decode-failed', ms: 40, codec: 'flac' }
	]);
	assert.equal(labels.stem_decode, 'mixed');
	assert.equal(labels.stem_decode_workers, '1/2');
	assert.equal(labels.stem_decode_refused, 'decode-failed');
});

test('a buffer is built at the decoded frame count, never past it', () => {
	const ctx = fakeContext(44100);
	// The decoder can report fewer samples than the channel array it filled.
	const built = decode.stemAudioBuffer(ctx, {
		channelData: [new Float32Array(200), new Float32Array(200)],
		samplesDecoded: 128,
		sampleRate: 44100
	});
	assert.equal(built.length, 128);
	assert.equal(built.numberOfChannels, 2);
	assert.equal(built.channels[0].length, 128, 'the tail past samplesDecoded is not audio');
});

test('a second load reuses the parked workers instead of spawning four more', async () => {
	const ctx = fakeContext();
	const { factory, state } = fakeDecoderFactory();
	const fallback = countingFallback();

	await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: factory,
		decodeFallback: fallback.fn
	});
	assert.equal(state.made, 4, 'the first load pays the spawn');

	// The barrier in the fake releases once four decodes are in flight; a
	// second load needs its own four.
	state.releases.length = 0;
	await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: factory,
		decodeFallback: fallback.fn
	});

	assert.equal(state.made, 4, 'the second load must spawn nothing');
	assert.equal(state.resets, 4, 'and must reset each decoder before reuse');
	assert.equal(decode.stemDecodeSession.pooled(), 4);
});

test('the pool never grows past the widest layout', async () => {
	const ctx = fakeContext();
	const { factory, state } = fakeDecoderFactory();
	// A layout wider than any real one: extra decoders must be freed, not parked.
	const wide = ['vocals', 'drums', 'bass', 'other', 'fifth', 'sixth'];
	const encoded = Object.fromEntries(wide.map((part) => [part, FLAC()]));
	const barrier = { ...state };
	void barrier;
	// Widen the fake's barrier to this layout so the decodes still release.
	const factoryWide = (() => {
		const s2 = { made: 0, freed: 0, resets: 0, live: 0, peakLive: 0, releases: [] };
		return {
			factory: () => {
				s2.made += 1;
				return {
					ready: Promise.resolve(),
					async decodeFile() {
						await new Promise((resolve) => {
							s2.releases.push(resolve);
							if (s2.releases.length === wide.length) for (const r of s2.releases) r();
						});
						return {
							channelData: [new Float32Array(64), new Float32Array(64)],
							samplesDecoded: 64,
							sampleRate: 44100
						};
					},
					async reset() {
						s2.resets += 1;
					},
					free() {
						s2.freed += 1;
					}
				};
			},
			state: s2
		};
	})();

	await decode.decodeStemParts(ctx, encoded, wide, {
		makeDecoder: factoryWide.factory,
		decodeFallback: countingFallback().fn
	});

	assert.equal(factoryWide.state.made, 6);
	assert.equal(decode.stemDecodeSession.pooled(), 4, 'four workers is the cap, not six');
	assert.equal(factoryWide.state.freed, 2, 'the surplus must be destroyed, not leaked');
	void factory;
});

//----------------------------------------------------------------- lane choice

/** A clock whose Nth call returns a fixed elapsed time for the whole load. */
function loadClock(perLoadMs) {
	const queue = [...perLoadMs];
	let current = 0;
	let elapsed = 0;
	return () => {
		// decodeStemParts reads the clock at load start, at each part start and
		// end, then at load end. Only the load-level delta is asserted on, so
		// the clock advances once per load and holds still inside it.
		if (current === 0) {
			current = 1;
			elapsed += 0;
			return 0;
		}
		return 0;
	};
}
void loadClock;

/** Run one whole load on whichever lane the module currently wants. */
async function trialLoad({ wallMs, concurrent }) {
	let t = 0;
	const now = () => t;
	const ctx = fakeContext();
	const { factory, state } = fakeDecoderFactory({ concurrent });
	const fallback = countingFallback();
	const promise = decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: factory,
		decodeFallback: fallback.fn,
		now: () => {
			const at = now();
			// The load-end read is the last one; advance the clock just before
			// it so the whole load measures exactly wallMs.
			return at;
		}
	});
	t = wallMs;
	const result = await promise;
	return { result, state, fallback };
}
void trialLoad;

test('the first two stemmed loads measure one lane each, whole and uncontended', async () => {
	// The rung wins on WebKit (1.63x) and LOSES on Chromium (0.58x), measured
	// on this repo's own fixtures. A hard-coded answer is right on one engine
	// and a regression on the other, and rots when either ships a decoder
	// change - which already happened to the bench's Chrome column.
	//
	// WHOLE loads, one lane each. An earlier version split a single load
	// between the lanes and was wrong: three saturated workers starve the
	// main-thread decode running beside them, so the main lane measured slow
	// for a reason unrelated to the question.
	decode.stemDecodeSession.resetLane();
	assert.equal(decode.stemDecodeSession.lane(PARTS.length), null, 'nothing may be assumed before a load');

	const ctx = fakeContext();
	// Load 1: main thread. What ships today, so a session with one stemmed
	// load pays nothing for the trial.
	const first = fakeDecoderFactory();
	const firstFallback = countingFallback();
	const one = await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: first.factory,
		decodeFallback: firstFallback.fn,
		now: stepClock(400)
	});
	assert.equal(first.state.made, 0, 'the first trial must not spawn a worker at all');
	assert.equal(firstFallback.calls.length, 4);
	assert.ok(one.reports.every((r) => r.refusal === 'calibrating'));
	assert.equal(decode.stemDecodeSession.lane(PARTS.length), null, 'one lane is not a comparison');

	// Load 2: workers.
	decode.stemDecodeSession.resetPool();
	const second = fakeDecoderFactory();
	const secondFallback = countingFallback();
	const two = await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: second.factory,
		decodeFallback: secondFallback.fn,
		now: stepClock(100)
	});
	assert.equal(second.state.made, 4, 'the second trial runs the other lane, whole');
	assert.deepEqual(secondFallback.calls, []);
	assert.ok(two.reports.every((r) => r.viaWorker));
	assert.equal(decode.stemDecodeSession.lane(PARTS.length), 'workers', 'the faster lane wins');
});

test('each stem layout calibrates on its own, so a four-part verdict never pins a two-part load', async () => {
	// Worker throughput depends on how many decodes overlap, and bytes per ms
	// does not normalize that width. A mixed library (demucs4 + roformer2)
	// once shared one verdict, so a four-part win could send two-part loads
	// down the lane that is slower for them for the rest of the session.
	const TWO = ['vocals', 'instrumental'];
	const twoFlac = () => Object.fromEntries(TWO.map((part) => [part, FLAC()]));
	const twoPartClock = (wholeLoadMs) => {
		let calls = 0;
		return () => ((calls += 1) >= 1 + 2 * TWO.length + 1 ? wholeLoadMs : 0);
	};
	decode.stemDecodeSession.resetLane();
	const ctx = fakeContext();
	await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: fakeDecoderFactory().factory,
		decodeFallback: countingFallback().fn,
		now: stepClock(400)
	});
	decode.stemDecodeSession.resetPool();
	await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: fakeDecoderFactory().factory,
		decodeFallback: countingFallback().fn,
		now: stepClock(100)
	});
	assert.equal(decode.stemDecodeSession.lane(PARTS.length), 'workers', 'four-part layout settled');
	assert.equal(decode.stemDecodeSession.lane(TWO.length), null, 'two-part layout still unmeasured');

	decode.stemDecodeSession.resetPool();
	const twoMain = fakeDecoderFactory();
	const twoMainRun = await decode.decodeStemParts(ctx, twoFlac(), TWO, {
		makeDecoder: twoMain.factory,
		decodeFallback: countingFallback().fn,
		now: twoPartClock(100)
	});
	assert.equal(twoMain.state.made, 0, 'the two-part layout runs its own main-thread trial');
	assert.ok(twoMainRun.reports.every((r) => r.refusal === 'calibrating'));

	decode.stemDecodeSession.resetPool();
	await decode.decodeStemParts(ctx, twoFlac(), TWO, {
		makeDecoder: fakeDecoderFactory({ concurrent: TWO.length }).factory,
		decodeFallback: countingFallback().fn,
		now: twoPartClock(110)
	});
	assert.equal(decode.stemDecodeSession.lane(TWO.length), 'main-thread', 'slower workers lose at width 2');
	assert.equal(decode.stemDecodeSession.lane(PARTS.length), 'workers', 'and width 4 keeps its own verdict');
});

test('an engine where the main thread wins never gets the workers again', async () => {
	decode.stemDecodeSession.resetLane();
	const ctx = fakeContext();
	await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: fakeDecoderFactory().factory,
		decodeFallback: countingFallback().fn,
		now: stepClock(100)
	});
	decode.stemDecodeSession.resetPool();
	await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: fakeDecoderFactory().factory,
		decodeFallback: countingFallback().fn,
		now: stepClock(400)
	});
	assert.equal(decode.stemDecodeSession.lane(PARTS.length), 'main-thread');

	// And the NEXT load must actually honor it - a measurement nobody spends
	// is the same as no measurement.
	decode.stemDecodeSession.resetPool();
	const { factory, state } = fakeDecoderFactory();
	const fallback = countingFallback();
	const next = await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: factory,
		decodeFallback: fallback.fn
	});
	assert.equal(state.made, 0, 'no worker may be spawned on an engine that lost');
	assert.equal(fallback.calls.length, 4);
	assert.ok(next.reports.every((r) => r.refusal === 'engine-prefers-main'));
	const labels = decode.stemDecodeLabels(next.reports);
	assert.equal(labels.stem_decode, 'main-thread');
	assert.equal(labels.stem_decode_lane, 'main-thread');
	assert.equal(labels.stem_decode_refused, 'engine-prefers-main');
});

test('a narrow win keeps the main thread, because the rung must EARN the switch', async () => {
	const settle = async (mainMs, workerMs) => {
		decode.stemDecodeSession.resetLane();
		decode.stemDecodeSession.resetPool();
		const ctx = fakeContext();
		await decode.decodeStemParts(ctx, allFlac(), PARTS, {
			makeDecoder: fakeDecoderFactory().factory,
			decodeFallback: countingFallback().fn,
			now: stepClock(mainMs)
		});
		decode.stemDecodeSession.resetPool();
		await decode.decodeStemParts(ctx, allFlac(), PARTS, {
			makeDecoder: fakeDecoderFactory().factory,
			decodeFallback: countingFallback().fn,
			now: stepClock(workerMs)
		});
		return decode.stemDecodeSession.lane(PARTS.length);
	};
	// 1.2x for the workers: real, inside the margin, incumbent keeps the lane.
	assert.equal(await settle(120, 100), 'main-thread');
	// POSITIVE CONTROL: past the margin it flips, so the refusal above is the
	// margin and not a lane that can never be won.
	assert.equal(await settle(200, 100), 'workers');
});

test('a load that refused the worker path does not count as a trial of it', async () => {
	decode.stemDecodeSession.resetLane();
	const ctx = fakeContext();
	await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: fakeDecoderFactory().factory,
		decodeFallback: countingFallback().fn,
		now: stepClock(400)
	});
	// The workers trial, but every part fails: this measured the FALLBACK, not
	// the workers, and recording it would settle the lane on a lie.
	decode.stemDecodeSession.resetPool();
	await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: fakeDecoderFactory({ fail: true }).factory,
		decodeFallback: countingFallback().fn,
		now: stepClock(50)
	});
	assert.equal(decode.stemDecodeSession.lane(PARTS.length), null, 'a failed lane is unmeasured, not fast');
});

//------------------------------------------------- lane trials under overlap

/**
 * A decoder whose ready / reset / free can each be steered.
 *
 * Separate from `fakeDecoderFactory` because these cases are about the
 * CHECKOUT failing, which happens before that one's barrier is reachable.
 */
function lifecycleDecoderFactory({ readyRejects = false, resetRejects = false } = {}) {
	const state = { made: 0, freed: 0, resets: 0 };
	const factory = () => {
		state.made += 1;
		return {
			ready: readyRejects
				? Promise.reject(new Error('wasm compile blocked'))
				: Promise.resolve(),
			async decodeFile() {
				return {
					channelData: [new Float32Array(64), new Float32Array(64)],
					samplesDecoded: 64,
					sampleRate: 44100
				};
			},
			async reset() {
				state.resets += 1;
				if (resetRejects) throw new Error('worker is gone');
			},
			free() {
				state.freed += 1;
			}
		};
	};
	return { factory, state };
}

// REQ: PERF-STEMDEC-01
test('a decoder that never becomes ready is freed, not leaked per load', async () => {
	// The leak this catches is not one worker: the failed checkout is not
	// recorded as a lane trial, so every later stem load retries and strands
	// four more worker threads and four more wasm heaps.
	const ctx = fakeContext();
	const { factory, state } = lifecycleDecoderFactory({ readyRejects: true });
	const fallback = countingFallback();

	const result = await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: factory,
		decodeFallback: fallback.fn
	});

	assert.equal(state.made, 4, 'the checkout got as far as constructing a decoder');
	assert.equal(state.freed, 4, 'and every one of them is released, not stranded');
	assert.equal(decode.stemDecodeSession.pooled(), 0, 'nor parked for reuse');
	assert.equal(fallback.calls.length, 4, 'the deck still loads at yesterday speed');
	assert.ok(result.reports.every((r) => r.refusal === 'decode-failed'));
});

// REQ: PERF-STEMDEC-01
test('a pooled decoder that cannot reset is freed, not returned to the pool', async () => {
	const ctx = fakeContext();
	// Park four healthy decoders first, then make reset reject on reuse.
	const healthy = lifecycleDecoderFactory();
	await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: healthy.factory,
		decodeFallback: countingFallback().fn
	});
	assert.equal(decode.stemDecodeSession.pooled(), 4, 'the pool is primed');

	const broken = lifecycleDecoderFactory({ resetRejects: true });
	// Rebuild the pool out of decoders whose reset rejects.
	decode.stemDecodeSession.resetPool();
	const primed = lifecycleDecoderFactory({ resetRejects: true });
	await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: primed.factory,
		decodeFallback: countingFallback().fn
	});
	assert.equal(decode.stemDecodeSession.pooled(), 4);

	const fallback = countingFallback();
	const result = await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: broken.factory,
		decodeFallback: fallback.fn
	});

	assert.equal(primed.state.resets, 4, 'each pooled decoder was asked to reset');
	assert.equal(primed.state.freed, 4, 'and each one that could not is destroyed');
	assert.equal(decode.stemDecodeSession.pooled(), 0, 'a decoder that cannot reset is not a decoder');
	assert.equal(fallback.calls.length, 4);
	assert.ok(result.reports.every((r) => r.refusal === 'decode-failed'));
});

// REQ: PERF-STEMDEC-01
test('a decode the decoder complained about is refused, and its worker discarded', async () => {
	// `decodeFile` RESOLVES on a damaged stream: it hands back the frames it
	// managed plus an `errors` array. Everything else about that result looks
	// healthy - right rate, real channel data, a plausible frame count - so
	// without this check a truncated part is published as audio.
	const ctx = fakeContext(44100);
	const withErrors = {
		channelData: [new Float32Array(64), new Float32Array(64)],
		samplesDecoded: 64,
		sampleRate: 44100,
		errors: [{ message: 'FLAC__STREAM_DECODER_ERROR_STATUS_LOST_SYNC' }]
	};
	assert.equal(decode.stemAudioBuffer(ctx, withErrors), 'decode-failed');
	assert.equal(ctx.created.length, 0, 'no buffer may be built from a complained-about decode');

	// POSITIVE CONTROL from the hypothesis: the claim is that the ERRORS
	// refused it, so the identical result with an empty array must be built.
	const clean = decode.stemAudioBuffer(ctx, { ...withErrors, errors: [] });
	assert.equal(typeof clean, 'object', 'an empty errors array is a healthy decode');
	assert.equal(clean.length, 64);

	// And the worker that reported it must not go back in the healthy pool.
	const state = { made: 0, freed: 0 };
	const result = await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: () => {
			state.made += 1;
			return {
				ready: Promise.resolve(),
				async decodeFile() {
					return { ...withErrors };
				},
				async reset() {},
				free() {
					state.freed += 1;
				}
			};
		},
		decodeFallback: countingFallback().fn
	});
	assert.ok(result.reports.every((r) => r.refusal === 'decode-failed'));
	assert.equal(state.freed, 4, 'a decoder that reported a stream error is destroyed');
	assert.equal(decode.stemDecodeSession.pooled(), 0, 'never parked for the next load');
});

test('an overlapping load neither becomes a trial nor contaminates the one running', async () => {
	// The failure this prevents is NOT symmetric noise. Two overlapping
	// main-thread loads both measure slow, an uncontended worker trial then
	// wins by default, and Chromium - where the workers are 1.7x SLOWER -
	// pins itself to them for the session.
	decode.stemDecodeSession.resetLane();
	const ctx = fakeContext();

	let releaseFirst;
	const held = new Promise((resolve) => (releaseFirst = resolve));
	const firstFallback = [];
	const first = decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: fakeDecoderFactory().factory,
		decodeFallback: async (bytes) => {
			firstFallback.push(bytes.byteLength);
			await held;
			return { numberOfChannels: 2, length: 99, sampleRate: 44100 };
		},
		now: stepClock(400)
	});
	// The claim is taken synchronously at entry, so by here the trial is live.
	assert.equal(decode.stemDecodeSession.trialing(), true, 'the first load claimed a trial');

	const overlapFallback = countingFallback();
	const overlap = await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: fakeDecoderFactory().factory,
		decodeFallback: overlapFallback.fn,
		now: stepClock(9999)
	});
	assert.equal(overlapFallback.calls.length, 4, 'the overlapping load runs the shipping lane');
	assert.ok(
		overlap.reports.every((r) => r.refusal === 'awaiting-calibration'),
		'and says so, rather than claiming an engine preference nothing measured'
	);

	releaseFirst();
	await first;
	assert.equal(decode.stemDecodeSession.trialing(), false, 'the claim is released');

	// The contended main-thread number was DISCARDED, so the next uncontended
	// load is still the main-thread trial rather than the worker one.
	decode.stemDecodeSession.resetPool();
	const after = fakeDecoderFactory();
	const afterFallback = countingFallback();
	await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: after.factory,
		decodeFallback: afterFallback.fn,
		now: stepClock(400)
	});
	assert.equal(after.state.made, 0, 'the main-thread lane is still unmeasured, so it runs again');
	assert.equal(afterFallback.calls.length, 4);
	assert.equal(decode.stemDecodeSession.lane(PARTS.length), null, 'one lane is still not a comparison');
});

test('a non-FLAC bundle is never recorded as the main-thread trial', async () => {
	// The worker lane can only ever trial FLAC - anything else refuses per
	// part and the trial is discarded as unclean. So a main-thread trial on an
	// AAC or OGG v3 bundle would settle the FLAC lane from a different codec.
	decode.stemDecodeSession.resetLane();
	const ctx = fakeContext();
	const oggBundle = Object.fromEntries(PARTS.map((part) => [part, OGG()]));

	const ogg = await decode.decodeStemParts(ctx, oggBundle, PARTS, {
		makeDecoder: fakeDecoderFactory().factory,
		decodeFallback: countingFallback().fn,
		now: stepClock(9999)
	});
	assert.ok(
		ogg.reports.every((r) => r.refusal === 'not-flac'),
		'the bundle is refused per part, as before'
	);

	// POSITIVE CONTROL from the hypothesis: the claim is that the CODEC kept
	// it out of the trial, so the identical run on FLAC must become one.
	decode.stemDecodeSession.resetPool();
	const flacRun = fakeDecoderFactory();
	await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: flacRun.factory,
		decodeFallback: countingFallback().fn,
		now: stepClock(400)
	});
	assert.equal(flacRun.state.made, 0, 'the FLAC bundle takes the untrialed main-thread lane');

	// Now the worker trial. If the OGG load had been recorded, the main-thread
	// lane would already be settled at its 9999ms throughput and this load
	// would be comparing FLAC workers against OGG on the main thread.
	decode.stemDecodeSession.resetPool();
	const workers = fakeDecoderFactory();
	await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: workers.factory,
		decodeFallback: countingFallback().fn,
		now: stepClock(100)
	});
	assert.equal(workers.state.made, 4, 'the second FLAC load is the worker trial');
	assert.equal(decode.stemDecodeSession.lane(PARTS.length), 'workers');
});

test('a non-FLAC bundle never even claims the trial slot', async () => {
	// The clean-report rule would discard such a trial after the fact anyway,
	// so this pins the OTHER half: the slot is never taken, which is what stops
	// a non-FLAC load from forcing a concurrent FLAC load out of being one.
	decode.stemDecodeSession.resetLane();
	const ctx = fakeContext();

	let release;
	const held = new Promise((resolve) => (release = resolve));
	const heldFallback = async () => {
		await held;
		return { numberOfChannels: 2, length: 99, sampleRate: 44100 };
	};

	const ogg = decode.decodeStemParts(
		ctx,
		Object.fromEntries(PARTS.map((part) => [part, OGG()])),
		PARTS,
		{ makeDecoder: fakeDecoderFactory().factory, decodeFallback: heldFallback }
	);
	assert.equal(
		decode.stemDecodeSession.trialing(),
		false,
		'a bundle the worker lane can never decode must not hold the trial slot'
	);
	release();
	await ogg;

	// POSITIVE CONTROL from the hypothesis: the claim is that the CODEC kept
	// the slot free, so the identical held load on FLAC must take it.
	let releaseFlac;
	const heldFlac = new Promise((resolve) => (releaseFlac = resolve));
	const flac = decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: fakeDecoderFactory().factory,
		decodeFallback: async () => {
			await heldFlac;
			return { numberOfChannels: 2, length: 99, sampleRate: 44100 };
		}
	});
	assert.equal(decode.stemDecodeSession.trialing(), true, 'a FLAC bundle does take it');
	releaseFlac();
	await flac;
});

/**
 * A fallback that DETACHES its input, exactly as `decodeAudioData` does.
 *
 * `countingFallback` does not, and that difference hid a real defect: a byte
 * read placed after the fallback saw a zero-length buffer and answered a
 * question about nothing. Every unit case passed; the live browser run is what
 * failed. So the honest fallback is this one, and it exists to make that class
 * of defect reachable from the unit suite.
 */
function detachingFallback() {
	const calls = [];
	return {
		calls,
		fn: async (bytes) => {
			calls.push(bytes.byteLength);
			// structuredClone with a transfer list detaches the source, which
			// is the same observable effect decodeAudioData has on its input.
			structuredClone(bytes, { transfer: [bytes] });
			return { numberOfChannels: 2, length: 99, sampleRate: 44100 };
		}
	};
}

test('a calibration load still settles the lane when the fallback detaches its input', async () => {
	// THE regression this pins: `decodeAudioData` detaches the ArrayBuffer it
	// is handed. Any byte read placed after it sees byteLength 0, so a sniff
	// written inline where its answer is used reported `not-flac` for real FLAC,
	// which made every main-thread trial UNCLEAN, which stopped the session
	// ever settling a lane at all. Both trials then ran the main thread and the
	// rung silently bought nothing.
	decode.stemDecodeSession.resetLane();
	const ctx = fakeContext();

	const firstFallback = detachingFallback();
	const one = await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: fakeDecoderFactory().factory,
		decodeFallback: firstFallback.fn,
		now: stepClock(400)
	});
	assert.equal(firstFallback.calls.length, 4, 'the trial ran on the main thread');
	assert.ok(
		one.reports.every((r) => r.refusal === 'calibrating'),
		`a detached buffer must not read as another codec: ${JSON.stringify(one.reports.map((r) => r.refusal))}`
	);
	assert.ok(
		one.reports.every((r) => r.bytes > 0),
		'and the encoded size must be captured before the detach, or the trial has no denominator'
	);

	// The main-thread lane is now MEASURED, so the next load is the worker
	// trial rather than a second main-thread one.
	decode.stemDecodeSession.resetPool();
	const second = fakeDecoderFactory();
	await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: second.factory,
		decodeFallback: detachingFallback().fn,
		now: stepClock(100)
	});
	assert.equal(second.state.made, 4, 'the second trial must run the OTHER lane');
	assert.equal(decode.stemDecodeSession.lane(PARTS.length), 'workers', 'and the session settles a lane');

	// NEGATIVE CONTROL: a genuinely non-FLAC bundle must still be caught, so
	// the fix is not "stop sniffing on this lane".
	decode.stemDecodeSession.resetLane();
	decode.stemDecodeSession.forceLane('main-thread');
	const ogg = await decode.decodeStemParts(
		ctx,
		Object.fromEntries(PARTS.map((part) => [part, OGG()])),
		PARTS,
		{ makeDecoder: fakeDecoderFactory().factory, decodeFallback: detachingFallback().fn }
	);
	assert.ok(ogg.reports.every((r) => r.refusal === 'not-flac'), 'real non-FLAC is still named');
});

test('a rejecting part does not release the lane while its siblings are still decoding', async () => {
	// `Promise.all` rejects on the FIRST rejection and its siblings keep running,
	// because they cannot be cancelled. Releasing the claim there hands the next
	// deck load an "uncontended" machine that is in fact still decoding, and it
	// times its calibration trial against those orphans.
	decode.stemDecodeSession.resetLane();
	decode.stemDecodeSession.forceLane('main-thread');
	const ctx = fakeContext();

	let releaseSiblings;
	const held = new Promise((resolve) => (releaseSiblings = resolve));
	let siblingsRunning = 0;
	let first = true;
	const failing = async (bytes) => {
		void bytes;
		if (first) {
			first = false;
			throw new Error('the native decoder rejected this part');
		}
		siblingsRunning += 1;
		await held;
		siblingsRunning -= 1;
		return { numberOfChannels: 2, length: 99, sampleRate: 44100 };
	};

	let settled = false;
	const load = decode
		.decodeStemParts(ctx, allFlac(), PARTS, {
			makeDecoder: fakeDecoderFactory().factory,
			decodeFallback: failing
		})
		.catch((exc) => {
			settled = true;
			return exc;
		});

	// Let the rejection propagate as far as it can while the siblings are held.
	await new Promise((resolve) => setImmediate(resolve));
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(siblingsRunning, 3, 'the three siblings are genuinely still in flight');
	assert.equal(settled, false, 'the load must not resolve or reject before they finish');
	assert.equal(
		decode.stemDecodeSession.loadsInFlight(),
		1,
		'and the load must still be counted as occupying the machine'
	);

	releaseSiblings();
	const exc = await load;
	assert.match(String(exc), /native decoder rejected/, 'the first failure is rethrown unchanged');
	assert.equal(siblingsRunning, 0);
	assert.equal(decode.stemDecodeSession.loadsInFlight(), 0, 'released only once all parts settled');
	assert.equal(decode.stemDecodeSession.trialing(), false, 'and no claim is left behind');
});

test('a failing native fallback reports its own error, not a detached retry of itself', async () => {
	// The worker refused (wrong rate), so the part goes to `decodeAudioData`.
	// That call DETACHES the bytes. If it then rejects from inside the try, the
	// catch retries the same fallback on a buffer that no longer has any bytes,
	// and the operator is shown "detached ArrayBuffer" for a file whose real
	// problem was the media. The first terminal error is the news.
	const ctx = fakeContext(44100);
	const { factory } = fakeDecoderFactory({ sampleRate: 48000 });
	const calls = [];
	const fallback = async (bytes) => {
		calls.push(bytes.byteLength);
		if (bytes.byteLength === 0) throw new Error('cannot decode a detached ArrayBuffer');
		structuredClone(bytes, { transfer: [bytes] });
		throw new Error('EncodingError: the media could not be decoded');
	};

	const exc = await decode
		.decodeStemParts(ctx, allFlac(), PARTS, { makeDecoder: factory, decodeFallback: fallback })
		.then(
			() => new Error('the load must not resolve when every part failed to decode'),
			(err) => err
		);

	assert.match(
		String(exc),
		/the media could not be decoded/,
		'the real media error must reach the caller'
	);
	assert.doesNotMatch(String(exc), /detached/, 'and must not be replaced by a retry artifact');
	assert.equal(calls.length, PARTS.length, 'one fallback attempt per part, never two');
	assert.ok(
		calls.every((length) => length > 0),
		'so no attempt is ever made against an already-detached buffer'
	);

	// CONTROL ON THE OVERSHOOT: "stop calling the fallback after a refusal"
	// satisfies the report above completely and turns every 48kHz bundle into a
	// failed deck load. A refusal whose fallback SUCCEEDS still resolves, on the
	// fallback's buffer, with the refusal recorded.
	// The refusal above settled the session on the main thread, so the control
	// re-forces the worker lane to exercise the same path as the case.
	decode.stemDecodeSession.resetPool();
	decode.stemDecodeSession.forceLane('workers');
	const healthy = countingFallback();
	const ok = await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: fakeDecoderFactory({ sampleRate: 48000 }).factory,
		decodeFallback: healthy.fn
	});
	assert.equal(healthy.calls.length, PARTS.length, 'the fallback still runs on a refusal');
	assert.ok(ok.reports.every((r) => r.refusal === 'sample-rate-mismatch' && !r.viaWorker));
});

test('a bundle at a rate this context cannot run never spends a worker on it', async () => {
	// THE double decode. decodeAudioData resamples and a WASM decoder does not,
	// so a 48kHz bundle in a 44.1kHz context can never take the worker path.
	// Learning that only after the decode meant decoding every part twice -
	// once in a worker, thrown away, once natively - and the header says it for
	// free.
	decode.stemDecodeSession.resetLane();
	const ctx = fakeContext(44100);
	const { factory, state } = fakeDecoderFactory({ sampleRate: 48000 });
	const fallback = countingFallback();

	const result = await decode.decodeStemParts(ctx, allAt(48000), PARTS, {
		makeDecoder: factory,
		decodeFallback: fallback.fn
	});

	assert.equal(state.made, 0, 'not one decoder is taken for a decode that must be discarded');
	assert.equal(fallback.calls.length, PARTS.length, 'one native decode per part, not two');
	assert.ok(result.reports.every((r) => r.refusal === 'sample-rate-mismatch' && !r.viaWorker));
	// And it is not a lane trial: a load that could never run the worker lane
	// cannot settle a comparison between the lanes.
	assert.equal(decode.stemDecodeSession.trialing(), false);
	assert.equal(decode.stemDecodeSession.lane(PARTS.length), null, 'no verdict from an ineligible bundle');

	// CONTROL ON THE OVERSHOOT: "bar anything whose header mentions a rate"
	// would satisfy the report above and delete the rung. A bundle disclosing
	// the CONTEXT's rate still takes the workers.
	decode.stemDecodeSession.forceLane('workers');
	decode.stemDecodeSession.resetPool();
	const matching = fakeDecoderFactory({ sampleRate: 44100 });
	const ok = await decode.decodeStemParts(ctx, allAt(44100), PARTS, {
		makeDecoder: matching.factory,
		decodeFallback: countingFallback().fn
	});
	assert.equal(matching.state.made, PARTS.length, 'an eligible bundle still runs in workers');
	assert.ok(ok.reports.every((r) => r.viaWorker && r.refusal === null));
});

test('a rate the header hid settles the lane, so the waste is paid once not forever', async () => {
	// The residual case: STREAMINFO said nothing (rate field zero = unknown),
	// so only the decoder can report the mismatch, and by then its decode is
	// already wasted. Without settling here that waste repeats on EVERY later
	// load: the refusal keeps the worker trial unclean, so it is never
	// recorded, so no verdict ever exists, so the next load tries the workers
	// again. That is the loop, and it is unbounded.
	decode.stemDecodeSession.resetLane();
	const ctx = fakeContext(44100);

	// Trial 1: the main-thread lane, clean, recorded.
	await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: fakeDecoderFactory().factory,
		decodeFallback: countingFallback().fn,
		now: stepClock(400)
	});
	assert.equal(decode.stemDecodeSession.lane(PARTS.length), null, 'one trial is not a comparison');

	// Trial 2: the worker lane, on a bundle whose rate the bytes did not
	// disclose and whose decode comes back at the wrong rate.
	const wrongRate = fakeDecoderFactory({ sampleRate: 48000 });
	const wasted = countingFallback();
	const second = await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: wrongRate.factory,
		decodeFallback: wasted.fn,
		now: stepClock(100)
	});
	assert.equal(wrongRate.state.made, PARTS.length, 'the worker decode did happen');
	assert.equal(wasted.calls.length, PARTS.length, 'and was thrown away for a native one');
	assert.ok(second.reports.every((r) => r.refusal === 'sample-rate-mismatch'));
	assert.equal(
		decode.stemDecodeSession.lane(PARTS.length),
		'main-thread',
		'so the lane settles rather than leaving the next load to repeat it'
	);
	assert.equal(
		decode.stemDecodeSession.lane(2),
		'main-thread',
		'for every layout: a rate the workers cannot serve is a property of the context'
	);

	// The load after it pays ONE decode per part, not two, and forever after.
	const third = fakeDecoderFactory({ sampleRate: 48000 });
	const cheap = countingFallback();
	const after = await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: third.factory,
		decodeFallback: cheap.fn
	});
	assert.equal(third.state.made, 0, 'no worker is spent once the lane has settled');
	assert.equal(cheap.calls.length, PARTS.length);
	assert.ok(after.reports.every((r) => r.refusal === 'engine-prefers-main'));
});

test('the worker trial times steady state, not the wasm compile it pays once', async () => {
	// The trial's verdict is PERMANENT for the session, and the worker trial is
	// the load that first builds the pool. Spawn plus wasm compile is a
	// one-time cost that no later load pays, so charging it to the one load
	// that decides the lane can settle the session on the slower lane for the
	// rest of the night. Not hypothetical: this repo's own live run had the
	// stopwatch at 1.71x for workers while the cold trial chose the main thread.
	decode.stemDecodeSession.resetLane();
	decode.stemDecodeSession.resetPool();
	const ctx = fakeContext();
	let clockCalls = 0;
	// 1 load-start + 2 per part + 1 load-end, so the last read is the only one
	// that reports elapsed time.
	const now = () => {
		clockCalls += 1;
		return clockCalls >= 2 * PARTS.length + 2 ? 100 : 0;
	};
	const inner = fakeDecoderFactory();
	const madeAtClockCall = [];
	const factory = () => {
		madeAtClockCall.push(clockCalls);
		return inner.factory();
	};

	// Trial one is the main thread, so trial two is the worker lane.
	await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: factory,
		decodeFallback: countingFallback().fn,
		now: stepClock(400)
	});
	assert.deepEqual(madeAtClockCall, [], 'the main-thread trial constructs no decoder at all');

	clockCalls = 0;
	const workerTrial = await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: factory,
		decodeFallback: countingFallback().fn,
		now
	});
	assert.ok(
		workerTrial.reports.every((r) => r.viaWorker),
		'this load is the worker trial'
	);
	assert.equal(madeAtClockCall.length, PARTS.length, 'and it is the load that builds the pool');
	assert.deepEqual(
		madeAtClockCall,
		PARTS.map(() => 0),
		'every decoder is spawned and compiled BEFORE the load clock is first read'
	);
	// CONTROL ON THE OVERSHOOT: warming must PRE-BUILD the pool, not build a
	// second set beside it. Four decoders exist in total, not eight.
	assert.equal(inner.state.made, PARTS.length, 'the warmed decoders are the ones used');
	assert.equal(decode.stemDecodeSession.pooled(), PARTS.length, 'and all four are parked after');
});

//-----------------------------------------------------------------------------
// MPEG eligibility (PERF-STEMDEC-03)

import { readFile as readFileAsync } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

function withMpegRung(testFn) {
	return async (t) => {
		decode.stemDecodeSession.setMpegRungShipped(true);
		try {
			await testFn(t);
		} finally {
			decode.stemDecodeSession.setMpegRungShipped(false);
		}
	};
}

const MPEG_FIXTURE = path.join(
	path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../../..'),
	'tests/fixtures/phase7-dedup/src-320.mp3'
);

let mpegBytes;

test('MPEG bytes are not-flac when the mpeg rung is not shipped (default ship gate)', async () => {
	mpegBytes = mpegBytes ?? new Uint8Array(await readFileAsync(MPEG_FIXTURE));
	decode.stemDecodeSession.resetLane();
	const ctx = fakeContext(22050);
	const { factory, state } = fakeDecoderFactory({ sampleRate: 22050 });
	const allMpeg = () => Object.fromEntries(PARTS.map((part) => [part, mpegBytes.buffer.slice(0)]));
	const result = await decode.decodeStemParts(ctx, allMpeg(), PARTS, {
		makeDecoder: factory,
		decodeFallback: countingFallback().fn
	});
	assert.equal(state.made, 0, 'ship gate off: MPEG must not take a decoder');
	assert.ok(result.reports.every((r) => r.refusal === 'not-flac'));
	assert.equal(decode.stemDecodeSession.lane(PARTS.length, 'mpeg'), null);
});

test('MPEG bytes take the worker path when the mpeg lane is forced', withMpegRung(async () => {
	mpegBytes = mpegBytes ?? new Uint8Array(await readFileAsync(MPEG_FIXTURE));
	const ctx = fakeContext(22050);
	const { factory, state } = fakeDecoderFactory({ sampleRate: 22050 });
	const allMpeg = () => Object.fromEntries(PARTS.map((part) => [part, mpegBytes.buffer.slice(0)]));
	const result = await decode.decodeStemParts(ctx, allMpeg(), PARTS, {
		makeDecoder: factory,
		decodeFallback: countingFallback().fn
	});
	assert.equal(state.made, 4);
	assert.ok(result.reports.every((r) => r.viaWorker && r.refusal === null));
	assert.equal(decode.stemDecodeLabels(result.reports).stem_decode_codec, 'mpeg');
}));

test('OGG is still not-flac and never claims an MPEG trial', withMpegRung(async () => {
	decode.stemDecodeSession.resetLane();
	const ctx = fakeContext();
	const oggBundle = Object.fromEntries(PARTS.map((part) => [part, OGG()]));
	const result = await decode.decodeStemParts(ctx, oggBundle, PARTS, {
		makeDecoder: fakeDecoderFactory().factory,
		decodeFallback: countingFallback().fn,
		now: stepClock(9999)
	});
	assert.ok(result.reports.every((r) => r.refusal === 'not-flac'));
	assert.equal(decode.stemDecodeSession.lane(PARTS.length, 'mpeg'), null);
}));

test('a FLAC 4-part verdict does not pin an MPEG 4-part load', withMpegRung(async () => {
	mpegBytes = mpegBytes ?? new Uint8Array(await readFileAsync(MPEG_FIXTURE));
	decode.stemDecodeSession.resetLane();
	const ctx = fakeContext();
	const first = fakeDecoderFactory();
	await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: first.factory,
		decodeFallback: countingFallback().fn,
		now: stepClock(400)
	});
	decode.stemDecodeSession.resetPool();
	const second = fakeDecoderFactory();
	await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: second.factory,
		decodeFallback: countingFallback().fn,
		now: stepClock(100)
	});
	assert.equal(decode.stemDecodeSession.lane(PARTS.length, 'flac'), 'workers');
	assert.equal(decode.stemDecodeSession.lane(PARTS.length, 'mpeg'), null);

	decode.stemDecodeSession.resetPool();
	const mpegCtx = fakeContext(22050);
	const mpegRun = fakeDecoderFactory({ sampleRate: 22050 });
	const allMpeg = () => Object.fromEntries(PARTS.map((part) => [part, mpegBytes.buffer.slice(0)]));
	await decode.decodeStemParts(mpegCtx, allMpeg(), PARTS, {
		makeDecoder: mpegRun.factory,
		decodeFallback: countingFallback().fn,
		now: stepClock(400)
	});
	assert.equal(mpegRun.state.made, 0, 'MPEG main-thread trial is independent of FLAC verdict');
}));

test('MPEG at the wrong rate is sample-rate-mismatch without taking a decoder', withMpegRung(async () => {
	mpegBytes = mpegBytes ?? new Uint8Array(await readFileAsync(MPEG_FIXTURE));
	const ctx = fakeContext(44100);
	const { factory, state } = fakeDecoderFactory({ sampleRate: 22050 });
	const allMpeg = () => Object.fromEntries(PARTS.map((part) => [part, mpegBytes.buffer.slice(0)]));
	const result = await decode.decodeStemParts(ctx, allMpeg(), PARTS, {
		makeDecoder: factory,
		decodeFallback: countingFallback().fn
	});
	assert.equal(state.made, 0);
	assert.ok(result.reports.every((r) => r.refusal === 'sample-rate-mismatch'));
}));
