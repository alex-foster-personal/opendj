// requirement: PREF-02
// [if] a thumbs click / feedback_mark dispatch fires [then] a detached snapshot of tracks, time, loop, and EQ is recorded without taking an audio-thread command queue
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let entry;

before(async () => {
	entry = await loadTypeScriptModule('tests/unit/fixtures/feedback-mark-entry.ts', {
		viteApiBase: 'https://pref-02.example.test'
	});
});

test('feedback_mark has a null command-queue scope for every vote', () => {
	assert.equal(entry.performanceCommandQueueScopes({ type: 'feedback_mark', vote: 'bad' }), null);
	assert.equal(entry.performanceCommandQueueScopes({ type: 'feedback_mark', vote: 'good' }), null);
	assert.equal(entry.performanceCommandQueueScopes({ type: 'feedback_mark', vote: 'great' }), null);
});

test('feedback_mark snapshots track, time, loop, and EQ and returns a detached clone', async () => {
	const originalFetch = globalThis.fetch;
	const posts = [];
	globalThis.fetch = async (input, init) => {
		const request = input instanceof Request ? input : new Request(input, init);
		if (
			request.method === 'POST' &&
			request.url.includes('/api/v1/feedback/performance-marks')
		) {
			const body = JSON.parse(await request.text());
			posts.push(body);
			return new Response(JSON.stringify({ count: 1, last_mark: body }), {
				status: 201,
				headers: { 'content-type': 'application/json' }
			});
		}
		throw new Error(`unexpected fetch ${request.method} ${request.url}`);
	};
	globalThis.window = {};
	const uninstall = entry.installPerformanceBrowserIpc();
	try {
		const deck1 = entry.deckStates[1];
		deck1.stable_id = 'pref-02-track';
		deck1.playing = true;
		deck1.audible = true;
		deck1.position_ms = 1234.5;
		deck1.loop = { in_ms: 1000, out_ms: 2000 };
		entry.deckStates[2].stable_id = null;
		Object.assign(entry.mixerState.channels[1], {
			eq_low: 0.2,
			eq_mid: 0.4,
			eq_high: 0.8
		});

		await entry.dispatchPerformanceCommand({ type: 'feedback_mark', vote: 'great' });

		assert.equal(posts.length, 1);
		const posted = posts[0];
		assert.equal(posted.vote, 'great');
		const postedDeck1 = posted.decks.find((deck) => deck.deck_id === 1);
		assert.equal(postedDeck1.stable_id, 'pref-02-track');
		assert.equal(postedDeck1.position_ms, 1234.5);
		assert.deepEqual(postedDeck1.loop, { in_ms: 1000, out_ms: 2000 });
		const postedChannel1 = posted.mixer.channels.find((channel) => channel.deck_id === 1);
		assert.equal(postedChannel1.eq_low, 0.2);
		assert.equal(postedChannel1.eq_mid, 0.4);
		assert.equal(postedChannel1.eq_high, 0.8);

		const last = entry.queryPerformanceState().feedback_marks.last_mark;
		assert.equal(last.vote, 'great');
		assert.equal(last.decks[0].stable_id, 'pref-02-track');
		assert.equal(last.decks[0].position_ms, 1234.5);
		assert.deepEqual(last.decks[0].loop, { in_ms: 1000, out_ms: 2000 });
		const queriedChannel1 = last.mixer.channels.find((channel) => channel.deck_id === 1);
		assert.equal(queriedChannel1.eq_low, 0.2);
		assert.equal(queriedChannel1.eq_mid, 0.4);
		assert.equal(queriedChannel1.eq_high, 0.8);
		last.decks[0].stable_id = 'mutated';
		assert.equal(
			entry.queryPerformanceState().feedback_marks.last_mark.decks[0].stable_id,
			'pref-02-track'
		);
	} finally {
		uninstall();
		globalThis.fetch = originalFetch;
		delete globalThis.window;
	}
});

test('_feedbackMark reads published deck and mixer state and does not write the engine', async () => {
	const source = await readFile(
		new URL('../../src/lib/rb/performance-ipc.svelte.ts', import.meta.url),
		'utf8'
	);
	const start = source.indexOf('function _feedbackMark');
	assert.ok(start >= 0, '_feedbackMark must exist');
	const nextFn = source.indexOf('\nfunction ', start + 1);
	assert.ok(nextFn > start, '_feedbackMark body must be bounded by the next function');
	const body = source.slice(start, nextFn);
	assert.match(body, /getDeckState/);
	assert.match(body, /mixerState/);
	assert.doesNotMatch(body, /engine\./);
	assert.doesNotMatch(body, /captureDeckAudio/);
	assert.doesNotMatch(body, /AudioContext/);
});

test('VibeMeter thumbs dispatch a voided feedback_mark without opening library UI', async () => {
	const source = await readFile(
		new URL('../../src/lib/components/rb/VibeMeter.svelte', import.meta.url),
		'utf8'
	);
	assert.match(source, /aria-label="thumbs down"/);
	assert.match(source, /aria-label="thumbs up"/);
	assert.match(source, /recordFeedback\('bad', event\)/);
	assert.match(source, /event\.shiftKey \? 'great' : 'good'/);
	assert.match(source, /runPerformanceCommandFromUi\(\{ type: 'feedback_mark', vote \}/);
	assert.match(source, /void runPerformanceCommandFromUi/);
	assert.doesNotMatch(source, /voteVibe\(/);
	assert.doesNotMatch(source, /goto\(/);
	assert.doesNotMatch(source, /TempoPref/);
});
