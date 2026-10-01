/**
 * PERF-STEMDEC-04: stem parts decode one at a time while a deck is audible.
 *
 * Measured Thu 1 Oct 2026 (ops/perf/stem-decode-under-playback-round-0): with
 * a deck playing, the four-wide decode burst made the audio callback late at
 * about 0.90 per second against a 0.10 per second background; one part at a
 * time measured about 0.19. The hold that used to stand in front of the
 * decode did not move that rate, so the width is the protection.
 *
 * What a node test can prove is the SHAPE: how many decodes are in flight at
 * once, in which order, under which probe. Whether the shape removes the late
 * callbacks is a live measurement and lives in the round reports.
 *
 * Regression lines:
 *   - if more than one part decodes at once while a deck plays then broken
 *   - if a silent rig decodes fewer than all parts at once then broken (control load regresses)
 *   - if a deck that starts playing mid-bundle does not narrow the parts not yet started then broken
 *   - if a narrowed load becomes or records a lane trial then broken (the verdict would time the width)
 *   - if a rejected part stops the parts after it from decoding then broken
 *   - if an invalid width decodes anything rather than failing then broken
 *   - if stem-graph decodes without the live-deck width, or app-init never arms the probe, then broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let widthModule;
let decode;
let shedModule;

before(async () => {
	widthModule = await loadTypeScriptModule('src/lib/player/decode/stem-decode-width.ts');
	decode = await loadTypeScriptModule('tests/live/stem-decode-harness-entry.ts');
	shedModule = await loadTypeScriptModule('src/lib/rb/stem-decode-shed.ts');
});

beforeEach(() => {
	decode.stemDecodeSession.resetPool();
	decode.stemDecodeSession.resetLane();
});

const PARTS = ['vocals', 'drums', 'bass', 'other'];

//------------------------------------------------- helpers

/** Runs that stay open until released, recording order and overlap. */
function gatedRuns() {
	const state = { live: 0, peak: 0, started: [], release: new Map() };
	const run = (item) =>
		new Promise((resolve, reject) => {
			state.live += 1;
			state.peak = Math.max(state.peak, state.live);
			state.started.push(item);
			state.release.set(item, (outcome = 'ok') => {
				state.live -= 1;
				if (outcome === 'ok') resolve(`decoded:${item}`);
				else reject(new Error(`failed:${item}`));
			});
		});
	return { state, run };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

function flacBytes() {
	const out = new Uint8Array(64);
	out.set([0x66, 0x4c, 0x61, 0x43], 0);
	return out.buffer;
}
const allFlac = () => Object.fromEntries(PARTS.map((part) => [part, flacBytes()]));

/** A `decodeAudioData` stand-in that reports how many calls overlapped. */
function overlapFallback() {
	const state = { live: 0, peak: 0, calls: 0 };
	const fn = async () => {
		state.calls += 1;
		state.live += 1;
		state.peak = Math.max(state.peak, state.live);
		await tick();
		state.live -= 1;
		return { length: 128, sampleRate: 44100, numberOfChannels: 2 };
	};
	return { state, fn };
}

/** A worker decoder stand-in that reports how many decodes overlapped. */
function overlapDecoderFactory() {
	const state = { live: 0, peak: 0, calls: 0 };
	const factory = () => ({
		ready: Promise.resolve(),
		async decodeFile() {
			state.calls += 1;
			state.live += 1;
			state.peak = Math.max(state.peak, state.live);
			await tick();
			state.live -= 1;
			return {
				channelData: [new Float32Array(128), new Float32Array(128)],
				samplesDecoded: 128,
				sampleRate: 44100
			};
		},
		async reset() {},
		free() {}
	});
	return { state, factory };
}

function fakeContext() {
	return {
		sampleRate: 44100,
		createBuffer(channels, frames, rate) {
			return { numberOfChannels: channels, length: frames, sampleRate: rate, copyToChannel() {} };
		}
	};
}

//------------------------------------------------- the width rule

test('a silent rig decodes every part at once; a playing deck narrows to one', () => {
	assert.equal(widthModule.STEM_DECODE_WIDTH_WHILE_PLAYING, 1);
	assert.equal(widthModule.stemDecodeWidth(4, false), 4, 'control: nothing playing keeps the fast shape');
	assert.equal(widthModule.stemDecodeWidth(2, false), 2);
	assert.equal(widthModule.stemDecodeWidth(4, true), 1);
	assert.equal(widthModule.stemDecodeWidth(1, true), 1);
});

test('a part count that is not a positive integer fails instead of picking a width', () => {
	for (const bad of [0, -1, 1.5, Number.NaN]) {
		assert.throws(() => widthModule.stemDecodeWidth(bad, true), RangeError, String(bad));
	}
});

//------------------------------------------------- the bounded settle

test('width 1 runs the items strictly one after another, in order', async () => {
	const { state, run } = gatedRuns();
	const pending = widthModule.settleWithWidth(PARTS, () => 1, run);
	await tick();
	assert.deepEqual(state.started, ['vocals'], 'only the first item may be in flight');
	for (const part of PARTS) {
		assert.equal(state.live, 1);
		state.release.get(part)();
		await tick();
	}
	const result = await pending;
	assert.equal(state.peak, 1, 'two decodes overlapping is the burst this exists to prevent');
	assert.deepEqual(state.started, PARTS);
	assert.equal(result.narrowest, 1);
	assert.deepEqual(
		result.settled.map((entry) => entry.value),
		PARTS.map((part) => `decoded:${part}`)
	);
});

test('overshoot control: full width starts every item before any finishes', async () => {
	const { state, run } = gatedRuns();
	const pending = widthModule.settleWithWidth(PARTS, () => PARTS.length, run);
	assert.deepEqual(state.started, PARTS, 'all four start synchronously, as Promise.allSettled did');
	assert.equal(state.peak, 4);
	for (const part of PARTS) state.release.get(part)();
	assert.equal((await pending).narrowest, 4);
});

test('width 2 keeps exactly two in flight', async () => {
	const { state, run } = gatedRuns();
	const pending = widthModule.settleWithWidth(PARTS, () => 2, run);
	await tick();
	assert.deepEqual(state.started, ['vocals', 'drums']);
	state.release.get('vocals')();
	await tick();
	assert.deepEqual(state.started, ['vocals', 'drums', 'bass']);
	state.release.get('drums')();
	state.release.get('bass')();
	await tick();
	state.release.get('other')();
	await pending;
	assert.equal(state.peak, 2);
});

test('a deck that starts playing mid-bundle narrows the parts not yet started', async () => {
	const { state, run } = gatedRuns();
	let playing = false;
	const pending = widthModule.settleWithWidth(
		PARTS,
		() => widthModule.stemDecodeWidth(2, playing),
		run
	);
	await tick();
	assert.deepEqual(state.started, ['vocals', 'drums'], 'two wide while silent');
	playing = true;
	state.release.get('vocals')();
	await tick();
	assert.deepEqual(state.started, ['vocals', 'drums'], 'one still in flight fills the narrowed width');
	state.release.get('drums')();
	await tick();
	assert.deepEqual(state.started, ['vocals', 'drums', 'bass']);
	state.release.get('bass')();
	await tick();
	state.release.get('other')();
	assert.equal((await pending).narrowest, 1);
});

test('a deck that stops mid-bundle widens the parts not yet started', async () => {
	const { state, run } = gatedRuns();
	let playing = true;
	const pending = widthModule.settleWithWidth(
		PARTS,
		() => widthModule.stemDecodeWidth(PARTS.length, playing),
		run
	);
	await tick();
	assert.deepEqual(state.started, ['vocals']);
	playing = false;
	state.release.get('vocals')();
	await tick();
	assert.deepEqual(state.started, PARTS, 'the remaining three start together');
	for (const part of PARTS.slice(1)) state.release.get(part)();
	assert.equal((await pending).narrowest, 1);
});

test('a rejected part is reported in place and the parts after it still run', async () => {
	const { state, run } = gatedRuns();
	const pending = widthModule.settleWithWidth(PARTS, () => 1, run);
	await tick();
	state.release.get('vocals')('fail');
	await tick();
	assert.deepEqual(state.started, ['vocals', 'drums'], 'the failure must not stall the queue');
	for (const part of PARTS.slice(1)) {
		state.release.get(part)();
		await tick();
	}
	const { settled } = await pending;
	assert.deepEqual(
		settled.map((entry) => entry.status),
		['rejected', 'fulfilled', 'fulfilled', 'fulfilled']
	);
	assert.match(settled[0].reason.message, /failed:vocals/);
});

test('a run that throws synchronously is a rejection, not an escape', async () => {
	const { settled } = await widthModule.settleWithWidth(
		['a', 'b'],
		() => 1,
		(item) => {
			if (item === 'a') throw new Error('sync');
			return Promise.resolve(item);
		}
	);
	assert.deepEqual(
		settled.map((entry) => entry.status),
		['rejected', 'fulfilled']
	);
});

test('an invalid width fails the settle and starts nothing', async () => {
	for (const bad of [0, 1.5, Number.NaN, -2]) {
		const { state, run } = gatedRuns();
		await assert.rejects(widthModule.settleWithWidth(PARTS, () => bad, run), RangeError);
		assert.deepEqual(state.started, [], `width ${bad} must not decode anything`);
	}
});

test('no items settles to an empty result', async () => {
	const result = await widthModule.settleWithWidth([], () => 1, async () => 'never');
	assert.deepEqual(result.settled, []);
});

//------------------------------------------------- decodeStemParts honors it

test('decodeStemParts on the main-thread lane decodes one part at a time when narrowed', async () => {
	decode.stemDecodeSession.forceLane('main-thread');
	const fallback = overlapFallback();
	const result = await decode.decodeStemParts(fakeContext(), allFlac(), PARTS, {
		decodeFallback: fallback.fn,
		width: () => 1
	});
	assert.equal(fallback.state.calls, 4, 'every part still decodes');
	assert.equal(fallback.state.peak, 1);
	assert.equal(result.width, 1);
	assert.deepEqual(Object.keys(result.buffers), PARTS);
	assert.deepEqual(
		result.reports.map((report) => report.part),
		PARTS
	);
});

test('decodeStemParts on the workers lane decodes one part at a time when narrowed', async () => {
	decode.stemDecodeSession.forceLane('workers');
	const workers = overlapDecoderFactory();
	const result = await decode.decodeStemParts(fakeContext(), allFlac(), PARTS, {
		makeDecoder: workers.factory,
		decodeFallback: overlapFallback().fn,
		width: () => 1
	});
	assert.equal(workers.state.calls, 4);
	assert.equal(workers.state.peak, 1);
	assert.equal(result.width, 1);
	assert.ok(result.reports.every((report) => report.viaWorker));
});

test('overshoot control: with no width given, all four parts overlap as before', async () => {
	decode.stemDecodeSession.forceLane('main-thread');
	const fallback = overlapFallback();
	const result = await decode.decodeStemParts(fakeContext(), allFlac(), PARTS, {
		decodeFallback: fallback.fn
	});
	assert.equal(fallback.state.peak, 4, 'a silent rig must keep the four-wide decode');
	assert.equal(result.width, 4);
});

test('a narrowed load is never a lane trial and leaves the verdict unmeasured', async () => {
	// Unsettled lane, eligible FLAC bundle: a full-width load WOULD be the
	// main-thread trial. The control below proves that, so the narrowed case
	// cannot pass merely because trials never start in this fixture.
	const narrowedLoad = await decode.decodeStemParts(fakeContext(), allFlac(), PARTS, {
		decodeFallback: overlapFallback().fn,
		makeDecoder: overlapDecoderFactory().factory,
		width: () => 1
	});
	assert.ok(
		narrowedLoad.reports.every((report) => report.refusal === 'awaiting-calibration'),
		JSON.stringify(narrowedLoad.reports.map((report) => report.refusal))
	);
	assert.equal(decode.stemDecodeSession.lane(4), null);

	const fullLoad = await decode.decodeStemParts(fakeContext(), allFlac(), PARTS, {
		decodeFallback: overlapFallback().fn,
		makeDecoder: overlapDecoderFactory().factory
	});
	assert.ok(
		fullLoad.reports.every((report) => report.refusal === 'calibrating'),
		'control: the same load at full width is the trial'
	);
});

test('a trial that a deck narrows halfway is not recorded as a measurement', async () => {
	let clock = 0;
	const now = () => (clock += 10);
	let reads = 0;
	// Full width at claim time and for the first part, then a deck starts.
	const width = () => ((reads += 1) <= 2 ? 4 : 1);
	const first = await decode.decodeStemParts(fakeContext(), allFlac(), PARTS, {
		decodeFallback: overlapFallback().fn,
		makeDecoder: overlapDecoderFactory().factory,
		now,
		width
	});
	assert.ok(first.reports.every((report) => report.refusal === 'calibrating'), 'it began as the trial');
	assert.equal(first.width, 1);
	// Had the narrowed trial been recorded, the next eligible load would trial
	// the WORKERS lane. It must trial main-thread again instead.
	const workers = overlapDecoderFactory();
	const second = await decode.decodeStemParts(fakeContext(), allFlac(), PARTS, {
		decodeFallback: overlapFallback().fn,
		makeDecoder: workers.factory,
		now
	});
	assert.equal(workers.state.calls, 0, 'the main-thread trial is still owed');
	assert.ok(second.reports.every((report) => report.refusal === 'calibrating'));
});

//------------------------------------------------- the live-deck probe

test('the live-deck probe is false until armed, follows the deck, and disarms with the shed', () => {
	const shed = { request() {}, sync() {}, pending: false, xrunsInWindow: false };
	shedModule.setEagerStemDecodeShed(null);
	assert.equal(shedModule.eagerStemDecodeIsLive(), false);
	let playing = false;
	shedModule.setEagerStemDecodeShed(shed, null, () => playing);
	assert.equal(shedModule.eagerStemDecodeIsLive(), false);
	playing = true;
	assert.equal(shedModule.eagerStemDecodeIsLive(), true);
	shedModule.setEagerStemDecodeShed(shed, null);
	assert.equal(shedModule.eagerStemDecodeIsLive(), false, 'no probe means no narrowing claim');
	shedModule.setEagerStemDecodeShed(null, null, () => true);
	assert.equal(shedModule.eagerStemDecodeIsLive(), false);
});

//------------------------------------------------- wiring

function source(relative) {
	return readFileSync(fileURLToPath(new URL(`../../${relative}`, import.meta.url)), 'utf8');
}

test('decodeStemBuffers decodes at the live-deck width and labels it', () => {
	const graph = source('src/lib/rb/stem-graph.ts');
	const body = graph.slice(
		graph.indexOf('export async function decodeStemBuffers'),
		graph.indexOf('export function createDefaultStemControls')
	);
	assert.ok(body.length > 0, 'if decodeStemBuffers cannot be located this guard asserts nothing');
	assert.match(body, /width: \(\) => stemDecodeWidth\(parts\.length, eagerStemDecodeIsLive\(\)\)/);
	assert.match(body, /decode_width: String\(decoded\.width\)/);
});

test('app-init arms the live-deck probe with the transport read', () => {
	const init = source('src/lib/rb/app-init.ts');
	assert.match(
		init,
		/setEagerStemDecodeShed\(\s*shed,[^;]*kernelPressureIsElevated\([^;]*,\s*anyDeckPlaying\s*\);/
	);
});
