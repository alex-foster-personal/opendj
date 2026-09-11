import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, beforeEach, describe, it } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const STUB_IPC = fileURLToPath(
	new URL('./fixtures/silence-dropout-stub-ipc.ts', import.meta.url)
);

let dropout;

before(async () => {
	dropout = await loadTypeScriptModule('src/lib/rb/silence-dropout.ts');
});

const BASE_DECKS = [
	{
		id: 1,
		playing: true,
		audible: true,
		bpm: 128,
		beat_sync_enabled: true,
		sync_mode: 'beat',
		sync_error: null,
		processor_error: null,
		is_master: true
	},
	{
		id: 2,
		playing: false,
		audible: false,
		bpm: 124,
		beat_sync_enabled: false,
		sync_mode: 'bar',
		sync_error: null,
		processor_error: null,
		is_master: false
	}
];

describe('diagnoseSilenceCause', () => {
	it('names error-severity xrun rows', () => {
		assert.equal(
			dropout.diagnoseSilenceCause({
				decks: BASE_DECKS,
				events: [{ t: 't', kind: 'xrun', message: '80 xrun(s) in 2000ms', severity: 'error' }]
			}),
			'xrun'
		);
	});

	it('ignores warn-severity xrun only', () => {
		assert.equal(
			dropout.diagnoseSilenceCause({
				decks: BASE_DECKS,
				events: [{ t: 't', kind: 'xrun', message: 'noise', severity: 'warn' }]
			}),
			'unknown'
		);
	});

	it('names processor_error on a deck', () => {
		const decks = [{ ...BASE_DECKS[1], processor_error: 'boom' }];
		assert.equal(dropout.diagnoseSilenceCause({ decks, events: [] }), 'processor-error');
	});

	it('names processor failed toast-error rows', () => {
		assert.equal(
			dropout.diagnoseSilenceCause({
				decks: BASE_DECKS,
				events: [
					{
						t: 't',
						kind: 'toast-error',
						message: 'Deck 1 processor failed - boom',
						severity: 'error'
					}
				]
			}),
			'processor-error'
		);
	});

	it('names stranded-follower perf rows', () => {
		assert.equal(
			dropout.diagnoseSilenceCause({
				decks: BASE_DECKS,
				events: [
					{
						t: 't',
						kind: 'stranded-follower',
						message: 'deck 2 abandoned after master 1 settled without a beatgrid',
						severity: 'error'
					}
				]
			}),
			'stranded-follower'
		);
	});

	it('names gridless sync_error on a follower', () => {
		const decks = [
			BASE_DECKS[0],
			{
				...BASE_DECKS[1],
				beat_sync_enabled: true,
				sync_error: 'deck 1 settled without a beatgrid - Beat Sync cannot phase-lock'
			}
		];
		assert.equal(dropout.diagnoseSilenceCause({ decks, events: [] }), 'stranded-follower');
	});

	it('falls back to unknown', () => {
		assert.equal(dropout.diagnoseSilenceCause({ decks: BASE_DECKS, events: [] }), 'unknown');
	});
});

describe('formatAudioCutToast', () => {
	it('includes decks, bpm, sync, cause, and last error message', () => {
		const toast = dropout.formatAudioCutToast({
			decks: [
				{ ...BASE_DECKS[0], playing: true, audible: true },
				{ ...BASE_DECKS[1], playing: true, audible: true, beat_sync_enabled: false }
			],
			cause: 'xrun',
			last_error: { kind: 'xrun', message: '80 xrun(s) in 2000ms' }
		});
		assert.match(toast, /decks=1,2/);
		assert.match(toast, /128/);
		assert.match(toast, /124/);
		assert.match(toast, /beat/);
		assert.match(toast, /off/);
		assert.match(toast, /cause=xrun/);
		assert.match(toast, /80 xrun\(s\)/);
	});

	it('does not emit timestamp-only last lines', () => {
		const toast = dropout.formatAudioCutToast({
			decks: [BASE_DECKS[0]],
			cause: 'unknown',
			last_error: { kind: 'x', message: '' }
		});
		assert.doesNotMatch(toast, /2026-09-11T00:00:00/);
	});
});

describe('planSilenceDropout', () => {
	it('stops every claimed-live deck and recovers AutoPlay when a next track exists', () => {
		const plan = dropout.planSilenceDropout({
			decks: [
				{ ...BASE_DECKS[0], playing: true, audible: true },
				{ ...BASE_DECKS[1], audible: true, playing: false }
			],
			events: [],
			autoplay_enabled: true,
			has_playable_next: true
		});
		assert.deepEqual(plan.stop_decks, [1, 2]);
		assert.equal(plan.autoplay_recover, true);
		assert.match(plan.cause_message, /cause=/);
	});

	it('does not recover when the queue is empty', () => {
		const plan = dropout.planSilenceDropout({
			decks: [BASE_DECKS[0]],
			events: [],
			autoplay_enabled: true,
			has_playable_next: false
		});
		assert.equal(plan.autoplay_recover, false);
	});
});

describe('audio-engine RAF feed pin', () => {
	it('keeps sampling while deckStates claim live without presented observation', () => {
		const source = readFileSync(
			fileURLToPath(new URL('../../src/lib/rb/audio-engine.svelte.ts', import.meta.url)),
			'utf8'
		);
		assert.match(
			source,
			/observation\?\.audible \|\| observation\?\.transport_pending \|\| deckStates\[deck\]\.playing \|\| deckStates\[deck\]\.audible/
		);
	});
});

describe('beatgrid-resync stranded follower pin', () => {
	it('records stranded-follower when abandoning gridless followers', () => {
		const source = readFileSync(
			fileURLToPath(new URL('../../src/lib/player/beatgrid-resync.ts', import.meta.url)),
			'utf8'
		);
		assert.match(source, /recordPerfEvent\(\s*'stranded-follower'/);
	});
});

describe('executeSilenceDropoutPlan honest stop', () => {
	let actHarness;

	before(async () => {
		actHarness = await loadTypeScriptModule('tests/unit/fixtures/silence-dropout-act-entry.ts', {
			alias: { '$lib/rb/performance-ipc.svelte': STUB_IPC }
		});
	});

	beforeEach(() => {
		actHarness.resetDispatchPerformanceCommandStub();
		for (const deck of [1, 2, 3, 4]) {
			const st = actHarness.deckStates[deck];
			st.playing = false;
			st.audible = false;
			st.transport_pending = false;
			st.stable_id = null;
		}
	});

	it('dispatches play:false for each stop_deck when pause succeeds', async () => {
		actHarness.deckStates[1].playing = true;
		actHarness.deckStates[1].audible = true;
		actHarness.deckStates[1].stable_id = 'deck-1-track';
		actHarness.deckStates[2].audible = true;
		actHarness.deckStates[2].stable_id = 'deck-2-track';
		actHarness.setDispatchPerformanceCommandStub(async () => {});

		const plan = actHarness.planSilenceDropout({
			decks: [
				{ ...BASE_DECKS[0], playing: true, audible: true },
				{ ...BASE_DECKS[1], playing: false, audible: true }
			],
			events: [],
			autoplay_enabled: false,
			has_playable_next: false
		});
		await actHarness.executeSilenceDropoutPlan(plan);

		const playStops = actHarness
			.getDispatchPerformanceCommandCalls()
			.filter((cmd) => cmd.type === 'play' && cmd.playing === false);
		assert.deepEqual(
			playStops.map((cmd) => cmd.deck),
			[1, 2]
		);
		assert.equal(actHarness.deckStates[1].stable_id, 'deck-1-track');
		assert.equal(actHarness.deckStates[2].stable_id, 'deck-2-track');
	});

	it('clears live flags without unload when pause dispatch throws', async () => {
		actHarness.deckStates[1].playing = true;
		actHarness.deckStates[1].audible = true;
		actHarness.deckStates[1].transport_pending = true;
		actHarness.deckStates[1].stable_id = 'keep-me';
		actHarness.setDispatchPerformanceCommandStub(async () => {
			throw new Error('pause: no track loaded');
		});

		await actHarness.executeSilenceDropoutPlan({
			stop_decks: [1],
			toast: 'AUDIO CUT decks=1',
			perf_kind: 'silent-while-playing',
			cause: 'unknown',
			cause_message: 'cause=unknown',
			autoplay_recover: false
		});

		const st = actHarness.deckStates[1];
		assert.equal(st.playing, false);
		assert.equal(st.audible, false);
		assert.equal(st.transport_pending, false);
		assert.equal(st.stable_id, 'keep-me');
		assert.equal(
			actHarness.getDispatchPerformanceCommandCalls().length,
			1,
			'honest stop still attempts pause before flag flip'
		);
	});
});
