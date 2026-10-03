/**
 * PLAYS-01 - Open DJ's own play counter.
 *
 * Regression lines:
 * - if a load posts before PLAY_THRESHOLD_S heard seconds then auditioning a
 *   track for a few seconds inflates its play count
 * - if a load posts more than once then one play counts as several
 * - if unheard time (fader down, pre-cue) is credited then a track nobody
 *   heard counts as played
 * - if a long gap between samples is credited in full then a throttled tab
 *   banks minutes it never observed
 * - if loading another track does not start a new play then the second track
 *   inherits the first one's seconds
 * - if a failed post is dropped then a network blip deletes the play; if it
 *   is retried with a NEW play_id then the server cannot dedupe it
 * - if the default post stops hitting POST /api/v1/tracks/{id}/plays then the
 *   counter runs and nothing is ever logged
 * - if /performance stops installing the counter then nothing counts at all
 * - if stop does not sample once more then a play that crossed the threshold
 *   since the last tick is lost when /performance unmounts
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://engine.example.test';

let mod;
let originalFetch;

before(async () => {
	originalFetch = globalThis.fetch;
	mod = await loadTypeScriptModule('src/lib/plays/play-counter.ts', { viteApiBase: API_BASE });
});

after(() => {
	globalThis.fetch = originalFetch;
});

/** A counter over a scripted deck 1, with every other deck empty. */
function harness({ post, retryLater } = {}) {
	const deck1 = { stable_id: 'sid-a', duration_ms: 240_000 };
	const world = { heard: true, t: 0 };
	const posts = [];
	let ids = 0;
	const counter = mod.createPlayCounter({
		sample: () => ({
			decks: {
				1: { ...deck1 },
				2: { stable_id: null },
				3: { stable_id: null },
				4: { stable_id: null }
			}
		}),
		heard: (_state, deck) => deck === 1 && world.heard,
		post:
			post ??
			(async (play) => {
				posts.push(play);
			}),
		now: () => world.t,
		newPlayId: () => `play-${++ids}`,
		retryLater
	});
	/** Advance the clock in 1 s samples. */
	const run = (seconds) => {
		for (let i = 0; i < seconds; i += 1) {
			world.t += 1000;
			counter.tick();
		}
	};
	return { counter, deck1, world, posts, run };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

test('a load posts exactly once, only after PLAY_THRESHOLD_S heard seconds', async () => {
	const h = harness();
	h.counter.tick(); // first sample opens the load; nothing credited yet
	h.run(mod.PLAY_THRESHOLD_S - 1);
	await settle();
	assert.equal(h.posts.length, 0, 'posted before the threshold');
	h.run(1);
	await settle();
	assert.equal(h.posts.length, 1);
	assert.deepEqual(
		{ ...h.posts[0], audibleS: Math.round(h.posts[0].audibleS) },
		{ stableId: 'sid-a', playId: 'play-1', audibleS: 60, deck: 1, durationMs: 240_000 }
	);
	h.run(300);
	await settle();
	assert.equal(h.posts.length, 1, 'one load must never count twice');
});

test('reloading the same track is a new play, not the old one continued', async () => {
	// The sampler may never see the deck empty between two loads of one
	// track; load_generation is what tells them apart.
	const h = harness();
	h.deck1.load_generation = 1;
	h.counter.tick();
	h.run(mod.PLAY_THRESHOLD_S);
	await settle();
	assert.equal(h.posts.length, 1);
	h.deck1.load_generation = 2;
	h.run(mod.PLAY_THRESHOLD_S - 5);
	await settle();
	assert.equal(h.posts.length, 1, 'the reload must not inherit the first load\'s heard time');
	h.run(10);
	await settle();
	assert.equal(h.posts.length, 2);
	assert.notEqual(h.posts[1].playId, h.posts[0].playId);
});

test('a fractional decoded duration is posted as whole milliseconds', async () => {
	// The server's duration_ms is an int: a float like 215040.00000000003
	// (AudioBuffer.duration * 1000) is a 422, and the play is never logged.
	const h = harness();
	h.deck1.duration_ms = 215_040.00000000003;
	h.counter.tick();
	h.run(mod.PLAY_THRESHOLD_S);
	await settle();
	assert.equal(h.posts.length, 1);
	assert.equal(h.posts[0].durationMs, 215_040);
	assert.ok(Number.isInteger(h.posts[0].durationMs));
});

test('unheard seconds are not credited, and a quiet stretch does not bridge the gap', async () => {
	const h = harness();
	h.counter.tick();
	h.run(30);
	h.world.heard = false; // fader down / pre-cue
	h.run(120);
	h.world.heard = true;
	h.run(1); // first heard sample after silence credits nothing
	h.run(29);
	await settle();
	assert.equal(h.posts.length, 0, 'silence was credited as play time');
	h.run(1);
	await settle();
	assert.equal(h.posts.length, 1);
});

test('a long gap between samples credits at most MAX_SAMPLE_GAP_MS', async () => {
	const h = harness();
	h.counter.tick();
	h.world.t += 10 * 60 * 1000; // a throttled tab wakes ten minutes later
	h.counter.tick();
	await settle();
	assert.equal(h.posts.length, 0, 'a single gap banked more than the cap');
	assert.equal(h.counter.status().decks[1].heard_s, mod.MAX_SAMPLE_GAP_MS / 1000);
});

test('loading another track starts a new play with a new id', async () => {
	const h = harness();
	h.counter.tick();
	h.run(40);
	h.deck1.stable_id = 'sid-b';
	h.run(40);
	await settle();
	assert.equal(h.posts.length, 0, 'the second track inherited the first track seconds');
	h.run(21);
	await settle();
	assert.equal(h.posts.length, 1);
	assert.equal(h.posts[0].stableId, 'sid-b');
	assert.equal(h.posts[0].playId, 'play-2');
});

test('a failed post retries with the SAME play_id, and success stops the retries', async () => {
	let fail = true;
	const seen = [];
	const h = harness({
		post: async (play) => {
			seen.push(play.playId);
			if (fail) throw new Error('network down');
		}
	});
	h.counter.tick();
	h.run(60);
	await settle();
	assert.equal(seen.length, 1);
	h.run(1);
	await settle();
	assert.equal(seen.length, 2, 'a failed post was dropped instead of retried');
	fail = false;
	h.run(1);
	await settle();
	h.run(5);
	await settle();
	assert.deepEqual(seen, ['play-1', 'play-1', 'play-1'], 'retries must reuse the play_id');
	assert.equal(h.counter.status().posted, 1);
});

test('a failed post is still retried after its deck unloads', async () => {
	let fail = true;
	const seen = [];
	const h = harness({
		post: async (play) => {
			seen.push(play.playId);
			if (fail) throw new Error('network down');
		}
	});
	h.counter.tick();
	h.run(60);
	await settle();
	assert.equal(seen.length, 1);
	// The deck empties before the retry: the heard play must not be lost.
	h.deck1.stable_id = null;
	fail = false;
	h.run(1);
	await settle();
	assert.deepEqual(seen, ['play-1', 'play-1'], 'the retry died with the unloaded deck');
	assert.equal(h.counter.status().posted, 1);
	h.run(5);
	await settle();
	assert.equal(seen.length, 2, 'a landed post was sent again');
});

test('a post that fails after the counter stops retries on its own, not on a tick', async () => {
	let fail = true;
	const seen = [];
	const later = [];
	const h = harness({
		post: async (play) => {
			seen.push(play.playId);
			if (fail) throw new Error('network down');
		},
		retryLater: (retry) => later.push(retry)
	});
	h.counter.tick();
	h.run(60);
	await settle();
	assert.equal(seen.length, 1);
	assert.equal(later.length, 0, 'a running counter must retry on its ticks, not on its own');
	// Leaving /performance: the failed play is sent at once, and its next
	// failure schedules its own retry since no tick will come.
	h.counter.stop();
	assert.equal(seen.length, 2, 'stop did not flush the play waiting to retry');
	await settle();
	assert.equal(later.length, 1, 'a failure after stop was left for a tick that never comes');
	fail = false;
	later.shift()();
	await settle();
	assert.deepEqual(seen, ['play-1', 'play-1', 'play-1']);
	assert.equal(h.counter.status().posted, 1);
	assert.equal(later.length, 0);
});

test('stopping between ticks credits the heard time since the last tick', async () => {
	const h = harness();
	h.counter.tick();
	h.run(mod.PLAY_THRESHOLD_S - 1);
	// The threshold is crossed half a sample later, then /performance unmounts.
	h.world.t += 1000;
	h.counter.stop();
	await settle();
	assert.equal(h.posts.length, 1, 'a play heard past the threshold was dropped on stop');
	assert.equal(Math.round(h.posts[0].audibleS), mod.PLAY_THRESHOLD_S);
});

test('stopping short of the threshold posts nothing, and a second stop is a no-op', async () => {
	const h = harness();
	h.counter.tick();
	h.run(mod.PLAY_THRESHOLD_S - 2);
	h.world.t += 1000;
	h.counter.stop();
	h.world.t += 5000;
	h.counter.stop();
	await settle();
	assert.equal(h.posts.length, 0, 'stop credited time past the moment it was called');
});

test('a stop whose final read fails keeps the plays already counted', async () => {
	const h = harness();
	h.counter.tick();
	h.run(mod.PLAY_THRESHOLD_S);
	const broken = mod.createPlayCounter({
		sample: () => {
			throw new Error('engine gone');
		},
		post: async () => {}
	});
	assert.doesNotThrow(() => broken.stop());
	h.counter.stop();
	await settle();
	assert.equal(h.posts.length, 1);
});

test('the default post hits POST /api/v1/tracks/{id}/plays with the wire body', async () => {
	const requests = [];
	globalThis.fetch = async (input, init) => {
		const request = input instanceof Request ? input : new Request(input, init);
		requests.push({ method: request.method, url: request.url, body: await request.text() });
		return new Response(JSON.stringify({}), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};
	let t = 0;
	const counter = mod.createPlayCounter({
		sample: () => ({ decks: { 1: { stable_id: 'sid/a', duration_ms: null } } }),
		heard: () => true,
		now: () => t,
		newPlayId: () => 'abcdef12'
	});
	for (let i = 0; i <= mod.PLAY_THRESHOLD_S; i += 1) {
		counter.tick();
		t += 1000;
	}
	await settle();
	await settle();
	assert.equal(requests.length, 1);
	assert.equal(requests[0].method, 'POST');
	assert.equal(requests[0].url, `${API_BASE}/api/v1/tracks/sid%2Fa/plays`);
	assert.deepEqual(JSON.parse(requests[0].body), {
		play_id: 'abcdef12',
		audible_s: 60,
		deck: 1,
		duration_ms: null
	});
});

test('/performance installs the play counter and tears it down', () => {
	const page = readFileSync(
		fileURLToPath(new URL('../../src/routes/performance/+page.svelte', import.meta.url)),
		'utf8'
	);
	assert.match(page, /const uninstallPlayCounter = installPlayCounter\(\);/);
	assert.match(page, /uninstallPlayCounter\(\);/);
});
