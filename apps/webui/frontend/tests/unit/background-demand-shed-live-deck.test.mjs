/**
 * PERFMODE-04 background demand shed keeps deck controls live: pressure shedding
 * defers background jobs only, and a deck control used during a shed episode runs
 * at once without draining, dropping or re-toasting the owed background work.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let gateModule;

before(async () => {
	gateModule = await loadTypeScriptModule('src/lib/rb/playing-gate.ts', {
		viteApiBase: 'https://playing-gate.example.test'
	});
});

function flush() {
	return new Promise((resolve) => setImmediate(resolve));
}

// Builds a two-deck rig: a shed whose isPlaying reads the decks, plus deck
// controls that act on the decks directly, never through the shed.
function makeRig({ jobs = ['library-poll-cadence'] } = {}) {
	const state = {
		decks: { A: { playing: true, cued: 0 }, B: { playing: false, cued: 0 } },
		pressureElevated: false,
		xruns: 0,
		notifications: [],
		runs: new Map(jobs.map((id) => [id, 0]))
	};
	const shed = gateModule.createBackgroundDemandShed({
		isPlaying: () => Object.values(state.decks).some((deck) => deck.playing),
		pressureElevated: () => state.pressureElevated,
		readXruns: () => state.xruns,
		jobs: jobs.map((id) => ({
			id,
			run: async () => {
				state.runs.set(id, (state.runs.get(id) ?? 0) + 1);
			}
		})),
		notify: (suggestion) => state.notifications.push(suggestion),
		schedule: (task) => task(),
		now: () => 1000,
		record: () => {}
	});
	const controls = {
		cue(deckId) {
			state.decks[deckId].cued += 1;
		},
		play(deckId) {
			state.decks[deckId].playing = true;
		},
		pause(deckId) {
			state.decks[deckId].playing = false;
		}
	};
	return { shed, state, controls };
}

test('entering the shed threshold defers background work while deck A plays', async () => {
	// Pressure crossing the threshold owes background work; nothing about the decks changes.
	const { shed, state } = makeRig();
	state.pressureElevated = true;
	shed.sync();
	shed.request('library-poll-cadence');
	await flush();
	assert.equal(state.runs.get('library-poll-cadence'), 0);
	assert.equal(shed.pending, true);
});

test('a deck control used while shed runs at once and leaves owed work owed', async () => {
	// Cue and play on deck B take effect synchronously during the shed episode.
	const { shed, state, controls } = makeRig();
	state.pressureElevated = true;
	shed.sync();
	shed.request('library-poll-cadence');
	assert.equal(state.notifications.length, 2);

	controls.cue('B');
	controls.play('B');

	shed.sync();
	await flush();
	assert.equal(state.runs.get('library-poll-cadence'), 0, 'a deck control is not a drain');
	assert.equal(shed.pending, true, 'owed background work is not dropped');
	assert.equal(state.notifications.length, 2, 'a deck control does not re-fire suggestions');
});

test('deck control ids cannot be routed through the shed request path', () => {
	// P0 transport and track-select stay off the shed even mid-episode.
	const { shed, state } = makeRig();
	state.pressureElevated = true;
	shed.sync();
	shed.request('library-poll-cadence');
	for (const id of gateModule.P0_NEVER_SHED) {
		assert.throws(() => shed.request(id), new RegExp(`P0 job ${id} cannot be shed`));
	}
	assert.equal(shed.pending, true);
	assert.equal(state.runs.get('library-poll-cadence'), 0);
});

test('pressure returning below the resume threshold drains once with a deck still live', async () => {
	// The drain is keyed off pressure, not off deck state: deck A plays throughout.
	const { shed, state, controls } = makeRig({
		jobs: ['library-poll-cadence', 'eager-stem-decode']
	});
	state.pressureElevated = true;
	shed.sync();
	shed.request('eager-stem-decode');
	controls.cue('B');
	shed.request('library-poll-cadence');
	shed.request('library-poll-cadence');

	state.pressureElevated = false;
	shed.sync();
	await flush();
	assert.equal(state.runs.get('library-poll-cadence'), 1);
	assert.equal(state.runs.get('eager-stem-decode'), 1);
	assert.equal(shed.pending, false);

	shed.request('library-poll-cadence');
	await flush();
	assert.equal(state.runs.get('library-poll-cadence'), 2, 'background work runs live again');
});

test('pausing the last live deck while shed releases owed work once', async () => {
	// A transport stop is the one deck action that ends the episode, because shedding is gated on playback.
	const { shed, state, controls } = makeRig();
	state.pressureElevated = true;
	shed.sync();
	shed.request('library-poll-cadence');

	controls.pause('A');
	shed.sync();
	await flush();
	assert.equal(state.runs.get('library-poll-cadence'), 1);
	assert.equal(shed.pending, false);
	shed.sync();
	await flush();
	assert.equal(state.runs.get('library-poll-cadence'), 1, 'owed work runs once, not per sync');
});
