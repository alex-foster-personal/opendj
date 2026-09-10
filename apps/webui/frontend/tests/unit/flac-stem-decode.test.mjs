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
	decode = await loadTypeScriptModule('src/lib/player/decode/flac-stem-decode.ts');
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

test('a decode at the wrong sample rate is refused, not resampled by hope', async () => {
	// THE trap: decodeAudioData resamples to the context rate and a WASM
	// decoder does not. Four 48kHz stems in a 44.1kHz context agree with each
	// other perfectly, so validateStemBufferAlignment passes them, and the
	// engine's stem/source comparison then fails a load that used to work.
	const ctx = fakeContext(44100);
	const { factory } = fakeDecoderFactory({ sampleRate: 48000 });
	const fallback = countingFallback();

	const result = await decode.decodeStemParts(ctx, allFlac(), PARTS, {
		makeDecoder: factory,
		decodeFallback: fallback.fn
	});

	assert.equal(fallback.calls.length, 4, 'every mismatched part must go the resampling path');
	assert.ok(result.reports.every((r) => r.refusal === 'sample-rate-mismatch'));
	assert.equal(ctx.created.length, 0, 'and no buffer at the wrong rate may be built at all');

	// POSITIVE CONTROL: the identical setup at the matching rate takes the
	// worker path, so the refusal above is the rate and not the harness.
	// The pool is cleared first because a refused-for-rate decoder is still
	// HEALTHY and gets parked - the control must exercise its own factory,
	// not inherit four decoders wired to the previous one's release barrier.
	decode.stemDecodeSession.resetPool();
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
		{ part: 'vocals', viaWorker: true, refusal: null, ms: 10 },
		{ part: 'drums', viaWorker: false, refusal: 'decode-failed', ms: 40 }
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
	assert.equal(decode.stemDecodeSession.lane(), null, 'nothing may be assumed before a load');

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
	assert.equal(decode.stemDecodeSession.lane(), null, 'one lane is not a comparison');

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
	assert.equal(decode.stemDecodeSession.lane(), 'workers', 'the faster lane wins');
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
	assert.equal(decode.stemDecodeSession.lane(), 'main-thread');

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
		return decode.stemDecodeSession.lane();
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
	assert.equal(decode.stemDecodeSession.lane(), null, 'a failed lane is unmeasured, not fast');
});
