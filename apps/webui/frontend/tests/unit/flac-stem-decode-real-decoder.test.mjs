import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, before, beforeEach, test } from 'node:test';

import { FLACDecoderWebWorker } from '@wasm-audio-decoders/flac';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Q18 rung 1 driven by the REAL decoder on REAL audio.
 *
 * The sibling suite (`flac-stem-decode.test.mjs`) drives the policy through an
 * injected decoder, because a real decoder cannot be asked to throw on demand,
 * to return zero samples, or to reset-reject. This suite exists so that the
 * claims which DO have a real subject are made against it: the production
 * `FLACDecoderWebWorker` from `@wasm-audio-decoders/flac`, in real worker
 * threads, decoding a real FLAC file tracked in this repository, through the
 * production `decodeStemParts` entry point with nothing injected but the
 * factory that names the real class.
 *
 * WHAT IS STILL ADAPTED, and why it is not a decoder fake: Node has no Web
 * Audio, so `BaseAudioContext` is supplied as a two-member host - a sample rate
 * and `createBuffer`. That is the SINK the decoded samples are written into,
 * not the thing under test, and it is the only part of the path a browser
 * supplies that Node cannot. The real Web Audio sink is covered by
 * `tests/live/stem-decode-workers.mjs`, which runs this same module in WebKit
 * and Chromium against `decodeAudioData` and now exits nonzero when neither
 * engine launches.
 *
 * Regression lines:
 * - if the real decoder's output is not accepted then the rung buys nothing on
 *   any engine and the deck silently decodes at yesterday's speed
 * - if a real 22.05kHz file decoded into a 44.1kHz context is ACCEPTED then the
 *   stems are right in samples and wrong in seconds, and the engine's own
 *   stem/source check fails a load that used to work
 * - if real non-FLAC bytes reach the FLAC decoder then a v3 bundle in another
 *   codec fails a deck load that is actually fine
 * - if the module reports more samples than the decoder decoded then the tail
 *   past the decode is silence presented as audio
 */

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '../../../../..');

/**
 * A real 3.0s 22.05kHz mono FLAC, tracked in this repository.
 *
 * Deliberately NOT 44.1kHz: the module's subtlest refusal is the sample-rate
 * one, and a fixture that already matches the usual context rate could not
 * exercise it with real bytes. Read-only here - the fixture belongs to the
 * dedup suite and is never rewritten by this one.
 */
const REAL_FLAC = path.join(REPO, 'tests/fixtures/phase7-dedup/src.flac');
/** Real audio that is NOT FLAC, for the container sniff. */
const REAL_WAV = path.join(REPO, 'tests/fixtures/phase7-dedup/src.wav');
const FIXTURE_RATE = 22050;
const PARTS = ['vocals', 'drums', 'bass', 'other'];

let decode;
let flacBytes;
let wavBytes;
/** Every real decoder this suite made, so the worker threads can be reclaimed. */
const made = [];

/** The real class, wrapped only to record what was made so it can be freed. */
function realDecoderFactory() {
	const decoder = new FLACDecoderWebWorker();
	made.push(decoder);
	return decoder;
}

/**
 * The Web Audio SINK, which Node does not have.
 *
 * Not a decoder and not a bypass: it receives what the real decoder produced
 * and answers the one question the module asks of a context, its sample rate.
 * `copyToChannel` asserts the same bound the real one enforces by throwing.
 */
function nodeAudioSink(sampleRate) {
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

/** The fallback lane, which in Node can only record that it was reached. */
function countingFallback() {
	const calls = [];
	return {
		calls,
		fn: async (bytes) => {
			calls.push(bytes.byteLength);
			return { fallback: true, numberOfChannels: 1, length: 1, sampleRate: FIXTURE_RATE };
		}
	};
}

function bundleOf(bytes) {
	// A fresh copy per part: the decoder receives a Uint8Array view and four
	// parts sharing one buffer would not prove four independent decodes.
	return Object.fromEntries(PARTS.map((part) => [part, bytes.slice().buffer]));
}

before(async () => {
	// UNAVAILABLE rather than skipped: a suite whose subject is missing has not
	// passed, and a green tick on zero real decodes is not evidence.
	for (const fixture of [REAL_FLAC, REAL_WAV]) {
		assert.ok(
			existsSync(fixture),
			`UNAVAILABLE: the real-audio fixture ${fixture} is missing, so nothing real was decoded`
		);
	}
	flacBytes = new Uint8Array(await readFile(REAL_FLAC));
	wavBytes = new Uint8Array(await readFile(REAL_WAV));
	decode = await loadTypeScriptModule('src/lib/player/decode/flac-stem-decode.ts');
});

beforeEach(() => {
	decode.stemDecodeSession.resetPool();
	decode.stemDecodeSession.resetLane();
	// These cases are about the real decoder, not about lane choice, so the
	// lane is settled up front. The calibration cases live in the policy suite.
	decode.stemDecodeSession.forceLane('workers');
});

// Worker threads outlive the test that made them, so Node would not exit.
after(async () => {
	decode.stemDecodeSession.resetPool();
	await Promise.all(made.map((decoder) => decoder.free()));
	made.length = 0;
});

//-----------------------------------------------------------------------------

test('the real decoder decodes four real FLAC parts through the production path', async () => {
	const ctx = nodeAudioSink(FIXTURE_RATE);
	const fallback = countingFallback();

	const result = await decode.decodeStemParts(ctx, bundleOf(flacBytes), PARTS, {
		makeDecoder: realDecoderFactory,
		decodeFallback: fallback.fn
	});

	assert.deepEqual(fallback.calls, [], 'the real decoder must carry every part, not the fallback');
	assert.ok(
		result.reports.every((r) => r.viaWorker && r.refusal === null),
		`every part must take the worker path: ${JSON.stringify(result.reports)}`
	);
	assert.equal(decode.stemDecodeLabels(result.reports).stem_decode, 'workers');

	// The samples are the file's, not a shape the harness chose. 3.0s at
	// 22050Hz is 66150 frames, and the fixture is mono.
	for (const part of PARTS) {
		const buffer = result.buffers[part];
		assert.equal(buffer.sampleRate, FIXTURE_RATE, `${part} keeps the file's own rate`);
		assert.equal(buffer.numberOfChannels, 1, `${part} keeps the file's own channel count`);
		assert.equal(buffer.length, 66150, `${part} is the whole 3.0s file`);
		assert.equal(
			buffer.channels[0].length,
			buffer.length,
			'no sample past the decode may be published'
		);
	}
});

test('the real decoder produces the same samples the real decoder produces alone', async () => {
	// A control on the SINK: whatever the module hands to createBuffer must be
	// what the decoder itself returns for these bytes, so the assertions above
	// are about the decode and not about the adapter around it.
	const alone = new FLACDecoderWebWorker();
	made.push(alone);
	await alone.ready;
	const direct = await alone.decodeFile(flacBytes.slice());

	const ctx = nodeAudioSink(FIXTURE_RATE);
	const result = await decode.decodeStemParts(ctx, bundleOf(flacBytes), PARTS, {
		makeDecoder: realDecoderFactory,
		decodeFallback: countingFallback().fn
	});

	assert.equal(direct.samplesDecoded, 66150, 'the decoder itself decodes the whole file');
	assert.deepEqual(direct.errors, [], 'a healthy file reports no decoder errors');
	const published = result.buffers.vocals.channels[0];
	assert.equal(published.length, direct.samplesDecoded);
	let maxAbsDiff = 0;
	for (let i = 0; i < direct.samplesDecoded; i += 97) {
		maxAbsDiff = Math.max(maxAbsDiff, Math.abs(published[i] - direct.channelData[0][i]));
	}
	assert.equal(maxAbsDiff, 0, 'the module must publish the decoder output unaltered');
});

test('a real 22.05kHz file is REFUSED by a 44.1kHz context, not resampled by hope', async () => {
	// THE trap, with real bytes: decodeAudioData resamples to the context rate
	// and this decoder does not. Four such stems agree with EACH OTHER, so
	// validateStemBufferAlignment passes them, and the engine's own stem/source
	// comparison then fails a load that would have worked.
	const ctx = nodeAudioSink(44100);
	const fallback = countingFallback();

	const result = await decode.decodeStemParts(ctx, bundleOf(flacBytes), PARTS, {
		makeDecoder: realDecoderFactory,
		decodeFallback: fallback.fn
	});

	assert.equal(fallback.calls.length, PARTS.length, 'every mismatched part takes the resampler');
	assert.ok(
		result.reports.every((r) => r.refusal === 'sample-rate-mismatch'),
		`the refusal must name the rate: ${JSON.stringify(result.reports.map((r) => r.refusal))}`
	);
	assert.equal(ctx.created.length, 0, 'and no buffer at the wrong rate may be built at all');

	// POSITIVE CONTROL, from the hypothesis rather than from unrelated audio:
	// the claim is that the RATE is what refused, so the identical bytes at the
	// matching rate must be accepted. If this also refused, the refusal above
	// would be the harness.
	decode.stemDecodeSession.resetPool();
	const matched = await decode.decodeStemParts(
		nodeAudioSink(FIXTURE_RATE),
		bundleOf(flacBytes),
		PARTS,
		{ makeDecoder: realDecoderFactory, decodeFallback: countingFallback().fn }
	);
	assert.ok(matched.reports.every((r) => r.viaWorker));
});

test('real non-FLAC audio never reaches the FLAC decoder', async () => {
	const ctx = nodeAudioSink(FIXTURE_RATE);
	const fallback = countingFallback();
	let constructed = 0;

	const result = await decode.decodeStemParts(ctx, bundleOf(wavBytes), PARTS, {
		makeDecoder: () => {
			constructed += 1;
			return realDecoderFactory();
		},
		decodeFallback: fallback.fn
	});

	assert.equal(constructed, 0, 'a FLAC-only decoder must never be built for non-FLAC bytes');
	assert.equal(fallback.calls.length, PARTS.length, 'the deck still loads');
	assert.ok(result.reports.every((r) => r.refusal === 'not-flac' && !r.viaWorker));
});

test('a second real load reuses the parked workers instead of spawning four more', async () => {
	const ctx = nodeAudioSink(FIXTURE_RATE);
	let constructed = 0;
	const counting = () => {
		constructed += 1;
		return realDecoderFactory();
	};

	await decode.decodeStemParts(ctx, bundleOf(flacBytes), PARTS, {
		makeDecoder: counting,
		decodeFallback: countingFallback().fn
	});
	assert.equal(constructed, PARTS.length, 'the first load pays the real spawn and wasm compile');
	assert.equal(decode.stemDecodeSession.pooled(), PARTS.length, 'and parks all four');

	await decode.decodeStemParts(ctx, bundleOf(flacBytes), PARTS, {
		makeDecoder: counting,
		decodeFallback: countingFallback().fn
	});
	assert.equal(constructed, PARTS.length, 'the second load must spawn no new worker at all');
	assert.equal(decode.stemDecodeSession.pooled(), PARTS.length);
});

test('two real loads running at once both complete, and neither becomes a lane trial', async () => {
	// Deck loads are allowed to overlap. Nothing here may deadlock on the pool,
	// and a contended load may not be recorded as a measurement of its lane.
	decode.stemDecodeSession.resetLane();
	const [a, b] = await Promise.all([
		decode.decodeStemParts(nodeAudioSink(FIXTURE_RATE), bundleOf(flacBytes), PARTS, {
			makeDecoder: realDecoderFactory,
			decodeFallback: countingFallback().fn
		}),
		decode.decodeStemParts(nodeAudioSink(FIXTURE_RATE), bundleOf(flacBytes), PARTS, {
			makeDecoder: realDecoderFactory,
			decodeFallback: countingFallback().fn
		})
	]);

	for (const run of [a, b]) {
		assert.equal(Object.keys(run.buffers).length, PARTS.length, 'both loads complete');
	}
	assert.equal(
		decode.stemDecodeSession.lane(),
		null,
		'two overlapping loads measured contention, not a lane'
	);
	assert.equal(decode.stemDecodeSession.trialing(), false, 'and neither left a claim behind');
});
