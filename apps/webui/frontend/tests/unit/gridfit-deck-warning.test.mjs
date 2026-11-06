// GRIDFIT-03: the deck's BEAT SYNC warning carries the engine's grid-vs-audio
// finding (src/lib/rb/grid-audio-fit.svelte.ts). The module under test is
// loaded for real; only the network edge (globalThis.fetch) is a recording
// stand-in, as the MIDI suites stand in for navigator.requestMIDIAccess.
//
// Regression lines:
// - if a wrong-BPM finding does not lead the warning then the drift is hidden
//   behind the shape sentence or not shown at all
// - if `shifted` warns on the deck then a library-only finding nags live
// - if a track is requested twice then every reactive read hits the engine
// - if the request is made before REQUEST_DELAY_MS then it races the deck load
// - if a failed request produces a warning then no-verdict reads as a bad grid
// - if a failed request is never asked again then a transient engine miss
//   stays silent until reload
// - if a cancelled delay still fetches then a replaced track is judged
// - if one waiter cancelling drops the other then two decks sharing a track
//   starve each other
// - if the deck effect tracks the cache then a pending write loops it and a
//   failed write retries without another load
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { afterEach, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let fit;
let calls;
let replies;
const realFetch = globalThis.fetch;

const WRONG = {
	stable_id: 'b'.repeat(40),
	verdict: 'wrong_bpm',
	reason: null,
	deck_warning: true,
	message:
		'Beatgrid tempo 127.00 BPM does not match the audio (about 128.00 BPM): Beat Sync will drift on this track.',
	grid_bpm: 127,
	audio_bpm: 128
};
const SHIFTED = {
	...WRONG,
	verdict: 'shifted',
	deck_warning: false,
	message: 'Beatgrid is offset from the kicks by about 30 ms.'
};

before(async () => {
	fit = await loadTypeScriptModule('src/lib/rb/grid-audio-fit.svelte.ts');
});

// Answers are kept per track for the module's life, so each stateful test
// below uses its own track id instead of a reset hook.
beforeEach(() => {
	calls = [];
	replies = new Map();
	globalThis.fetch = async (url) => {
		calls.push({ url: String(url), at: performance.now() });
		const id = String(url).split('/beatgrid-flags/')[1].split('/')[0];
		const body = replies.get(id);
		if (body instanceof Error) return new Response('engine said no', { status: 500 });
		return new Response(JSON.stringify(body), {
			status: 200,
			headers: { 'Content-Type': 'application/json' }
		});
	};
});

afterEach(() => {
	globalThis.fetch = realFetch;
});

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

test('a wrong-BPM finding leads the warning; shifted and no answer add nothing', () => {
	const shape = 'Beatgrid: 12 of 400 intervals uneven - Beat Sync may wander on this track';
	assert.equal(fit.deckGridWarningText(null, WRONG), WRONG.message);
	assert.equal(fit.deckGridWarningText(shape, WRONG), `${WRONG.message} | ${shape}`);
	assert.equal(
		fit.deckGridWarningText(shape, SHIFTED),
		shape,
		'shifted is a library finding, never a deck warning'
	);
	assert.equal(fit.deckGridWarningText(null, SHIFTED), null);
	assert.equal(fit.deckGridWarningText(shape, null), shape);
	assert.equal(fit.deckGridWarningText(null, null), null);
});

test('one request per track, made only after the delay, and its answer is kept', async () => {
	replies.set(WRONG.stable_id, WRONG);
	const started = performance.now();
	fit.requestGridAudioFit(WRONG.stable_id);
	fit.requestGridAudioFit(WRONG.stable_id);
	assert.equal(calls.length, 0, 'asked during the deck load');
	await wait(fit.REQUEST_DELAY_MS + 100);
	assert.equal(calls.length, 1, 'asked more than once for one track');
	assert.ok(
		calls[0].at - started >= fit.REQUEST_DELAY_MS - 5,
		`asked after ${calls[0].at - started} ms`
	);
	assert.match(calls[0].url, /\/api\/v1\/beatgrid-flags\/b{40}\/audio-fit$/);
	assert.deepEqual(fit.gridAudioFitFor(WRONG.stable_id), WRONG);
	fit.requestGridAudioFit(WRONG.stable_id);
	await wait(fit.REQUEST_DELAY_MS + 100);
	assert.equal(calls.length, 1, 'a judged track was asked again');
});

test('a failed request gives no answer and so no warning', async () => {
	const id = 'c'.repeat(40);
	replies.set(id, new Error('boom'));
	const logged = [];
	const realError = console.error;
	console.error = (...args) => logged.push(args.join(' '));
	try {
		fit.requestGridAudioFit(id);
		await wait(fit.REQUEST_DELAY_MS + 100);
	} finally {
		console.error = realError;
	}
	assert.equal(fit.gridAudioFitFor(id), null);
	assert.equal(fit.deckGridWarningText(null, fit.gridAudioFitFor(id)), null);
	assert.ok(
		logged.some((line) => line.includes('[grid-audio-fit] could not judge')),
		'a failure must be logged, not silent'
	);
});

test('after a failed request, the next call for the same id schedules a new request', async () => {
	const id = 'd'.repeat(40);
	replies.set(id, new Error('boom'));
	const realError = console.error;
	console.error = () => {};
	try {
		fit.requestGridAudioFit(id);
		await wait(fit.REQUEST_DELAY_MS + 100);
		assert.equal(fit.gridAudioFitFor(id), null);
		assert.equal(calls.length, 1);
		replies.set(id, { ...WRONG, stable_id: id });
		fit.requestGridAudioFit(id);
		await wait(fit.REQUEST_DELAY_MS + 100);
	} finally {
		console.error = realError;
	}
	assert.equal(calls.length, 2);
	assert.deepEqual(fit.gridAudioFitFor(id), { ...WRONG, stable_id: id });
});

test('a request cancelled before REQUEST_DELAY_MS elapses sends no fetch', async () => {
	const id = 'e'.repeat(40);
	replies.set(id, { ...WRONG, stable_id: id });
	const cancel = fit.requestGridAudioFit(id);
	cancel();
	await wait(fit.REQUEST_DELAY_MS + 100);
	assert.equal(calls.length, 0);
	fit.requestGridAudioFit(id);
	await wait(fit.REQUEST_DELAY_MS + 100);
	assert.equal(calls.length, 1);
});

test('a done entry is not re-requested', async () => {
	const id = 'f'.repeat(40);
	const body = { ...WRONG, stable_id: id };
	replies.set(id, body);
	fit.requestGridAudioFit(id);
	await wait(fit.REQUEST_DELAY_MS + 100);
	assert.equal(calls.length, 1);
	assert.deepEqual(fit.gridAudioFitFor(id), body);
	fit.requestGridAudioFit(id);
	await wait(fit.REQUEST_DELAY_MS + 100);
	assert.equal(calls.length, 1);
});

test('the last waiter cancelling still leaves a remaining waiter fetching', async () => {
	const id = 'g'.repeat(40);
	replies.set(id, { ...WRONG, stable_id: id });
	const a = fit.requestGridAudioFit(id);
	fit.requestGridAudioFit(id);
	a();
	await wait(fit.REQUEST_DELAY_MS + 100);
	assert.equal(calls.length, 1, 'the remaining waiter still wanted the judgment');
});

test('SOURCE: the deck grid-fit effect tracks only stable_id and anlz-present', () => {
	const source = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/deck/DeckHeader.svelte', import.meta.url)),
		'utf8'
	);
	assert.match(source, /import \{[^}]*\buntrack\b[^}]*\} from 'svelte'/);
	assert.match(source, /const gridFitAnlzPresent = \$derived\(deck\.anlz !== null\);/);
	const effects = source.match(/\$effect\(\(\) => \{[\s\S]*?\n\t\}\);/g) ?? [];
	const gridFit = effects.filter((e) => e.includes('requestGridAudioFit'));
	assert.equal(gridFit.length, 1, 'exactly one effect schedules the grid-fit request');
	assert.match(
		gridFit[0],
		/const id = deck\.stable_id;\s*if \(id === null \|\| !gridFitAnlzPresent\) return;\s*return untrack\(\(\) => requestGridAudioFit\(id\)\);/
	);
	assert.equal(
		source.includes('return requestGridAudioFit(id);'),
		false,
		'a tracked requestGridAudioFit call must not sneak back'
	);
	assert.match(
		source,
		/const gridWarning: string \| null = \$derived\(\s*gridless \? null : deckGridWarningText\(gridShapeWarning, gridAudioFitFor\(deck\.stable_id\)\)/
	);
});
