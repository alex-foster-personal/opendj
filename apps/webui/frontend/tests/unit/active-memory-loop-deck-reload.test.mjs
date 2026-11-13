// requirement: CUES-9967
// [if] a deck loads a track with one active memory loop [then] the deck's safety slot is armed with that loop's stored range, [else stop].
// [if] the deck unloads and loads that track again [then] the slot is re-armed from the persisted cue, [else stop].
// [if] the loop is explicitly exited [then] the slot only disarms and the persisted active_loop flag survives, so the next load arms it again, [else stop].
/**
 * CUES-9967 (issue #13201): the active memory loop survives a deck reload.
 *
 * Drives the REAL engine through real-engine-rig.mjs. The rig serves /anlz with
 * no cues, so this file wraps the rig's fetch and adds ONE active memory loop to
 * every /anlz answer: that is the persisted metadata. Any request that is not a
 * GET is recorded, so a write to the stored cue would be seen.
 */
import assert from 'node:assert/strict';
import { afterEach, before, beforeEach, describe, it } from 'node:test';

import { bundleRealEngine, startRealEngine } from './real-engine-rig.mjs';

// The engine reads the page URL for ?extroute=; these tests run as a page with no query.
globalThis.location ??= { search: '' };

const SID = 'c'.repeat(40);

/** The persisted active memory loop, inside the rig's 4 s track. */
const ACTIVE_LOOP = Object.freeze({
	kind: 'loop',
	slot: null,
	in_ms: 1000,
	out_ms: 3000,
	is_loop: true,
	active_loop: true,
	beat_loop_size: null,
	color_table_index: null,
	comment: null
});

/** The slot a load must arm from ACTIVE_LOOP: the stored range, engaged as saved. */
const ARMED = { in_ms: 1000, out_ms: 3000, beat_length: null, armed: true };

/** @type {string} */
let bundleText;
/** @type {Awaited<ReturnType<typeof startRealEngine>>} */
let rig;
/** Every non-GET request the engine made: a write to persisted metadata would land here. */
let writes;
/** How many times /anlz was served, so a reload is known to read the persisted cue. */
let anlzServed;

const deck = () => rig.deck(1);

/** Wrap the rig's fetch so /anlz carries the persisted active memory loop. */
function servePersistedActiveLoop() {
	const rigFetch = globalThis.fetch;
	globalThis.fetch = async (input, init) => {
		const url = input instanceof Request ? input.url : String(input);
		const method = (
			init?.method ?? (input instanceof Request ? input.method : 'GET')
		).toUpperCase();
		if (method !== 'GET') writes.push(`${method} ${url}`);
		const response = await rigFetch(input, init);
		if (!new URL(url).pathname.endsWith('/anlz')) return response;
		anlzServed += 1;
		const body = await response.json();
		return new Response(JSON.stringify({ ...body, cues: [{ ...ACTIVE_LOOP }] }), {
			status: response.status,
			headers: { 'content-type': 'application/json' }
		});
	};
}

/** The active memory loop as the loaded deck holds it in its analysis cues. */
const deckActiveCue = () =>
	(deck().anlz?.cues ?? []).find((cue) => cue.kind === 'loop' && cue.active_loop);

describe(
	'the active memory loop survives a deck reload (CUES-9967)',
	{ concurrency: false },
	() => {
		before(async () => {
			bundleText = await bundleRealEngine();
		});

		beforeEach(async () => {
			writes = [];
			anlzServed = 0;
			rig = await startRealEngine(bundleText, {
				tracks: { [SID]: { title: 'Active Loop Track' } }
			});
			servePersistedActiveLoop();
		});

		afterEach(async () => {
			await rig.stop();
		});

		it('arms the slot on load and re-arms it after an unload and a second load', async () => {
			// If loading a track with one active memory loop does not arm that stored range, re-arming has nothing to prove.
			await rig.engine.load(1, SID, { stems: false });
			assert.ok(anlzServed >= 1, 'precondition: the load read the persisted cues');
			assert.deepEqual(
				deck().safety_loop,
				ARMED,
				'if the first load does not arm the active memory loop then broken'
			);

			await rig.engine.unload(1);
			assert.equal(deck().stable_id, null, 'precondition: the deck is empty');
			assert.equal(deck().safety_loop, null, 'precondition: an empty deck holds no safety slot');

			await rig.engine.load(1, SID, { stems: false });
			assert.deepEqual(
				deck().safety_loop,
				ARMED,
				'if reloading the track does not re-arm the slot from the persisted active loop then broken'
			);
			assert.deepEqual(
				deckActiveCue(),
				ACTIVE_LOOP,
				'if the reload changed the stored cue then broken'
			);
			assert.deepEqual(writes, [], 'if loading wrote to the stored cue then broken');
		});

		it('an explicit exit disarms only, keeps the persisted flag, and the next load arms again', async () => {
			// If an explicit loop exit erases the persisted active_loop flag, the next load can no longer re-arm it.
			await rig.engine.load(1, SID, { stems: false });
			assert.deepEqual(deck().safety_loop, ARMED, 'precondition: the load armed the slot');

			await rig.engine.setLoop(1, null);
			assert.deepEqual(
				deck().safety_loop,
				{ ...ARMED, armed: false },
				'if an explicit exit clears the endpoints instead of only disarming then broken'
			);
			assert.deepEqual(
				deckActiveCue(),
				ACTIVE_LOOP,
				'if an explicit exit erased the active_loop flag then broken'
			);
			assert.deepEqual(writes, [], 'if an explicit exit wrote to the stored cue then broken');

			await rig.engine.unload(1);
			await rig.engine.load(1, SID, { stems: false });
			assert.deepEqual(
				deck().safety_loop,
				ARMED,
				'if a load after an explicit exit does not re-arm from the persisted flag then broken'
			);
			assert.deepEqual(
				writes,
				[],
				'if the reload after an exit wrote to the stored cue then broken'
			);
		});
	}
);
