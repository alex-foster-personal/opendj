/**
 * Issue #8747: session restore must not fail-toast when stem replay hits a brief
 * `loading` window after the first saved stem command engages held-neutral stems.
 *
 * requirement: STEM-9236
 * [if] restore runs while stems return to `loading` between saved commands [then]
 *   every saved stem command dispatches and no session-restore error toast starts
 *   with `session restore deck`
 * [if] stems settle `error` before replay [then] a session-restore failure toast still appears
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let session;
let snapshot;
let stores;

before(async () => {
	({ session, snapshot, stores } = await loadTypeScriptModule(
		'tests/unit/fixtures/performance-session-restore-entry.ts'
	));
});

const WITHIN_MS = 5000;

function withinRealMs(promise, ms, what) {
	let timer;
	const guard = new Promise((_, reject) => {
		timer = setTimeout(() => reject(new Error(`${what}: no answer within ${ms} real ms`)), ms);
	});
	return Promise.race([promise, guard]).finally(() => clearTimeout(timer));
}

function mixerChannel() {
	return { trim: 0.5, eq_high: 0.5, eq_mid: 0.5, eq_low: 0.5, filter: 0.5, fader: 1, assign: 'THRU' };
}

function deckRow(stable_id) {
	return {
		stable_id,
		position_ms: 0,
		pitch: 1,
		pitch_range: 8,
		quantize_enabled: true,
		beat_sync_enabled: false,
		master_tempo_enabled: true,
		key_sync_enabled: false,
		playing: false
	};
}

function stemsSnapshotNeutral() {
	return {
		vocal: { muted: false, solo: false, gain: 0.5 },
		instrumental: { muted: false, solo: false, gain: 0.5 },
		drums: { muted: false, solo: false, gain: 0.5 }
	};
}

const TRACK_ID = 'f'.repeat(40);

/** Empty URL: restore uses snapshot ids only. */
const LOCATION = {
	pathname: '/performance',
	search: '',
	href: 'http://127.0.0.1/performance'
};

function snapshotInput() {
	const captured_at_ms = Date.now();
	return {
		captured_at_ms,
		playlist_id: null,
		master_deck: null,
		decks: {
			1: deckRow(TRACK_ID),
			2: deckRow(null),
			3: deckRow(null),
			4: deckRow(null)
		},
		mixer: {
			crossfader: 0.5,
			master: 0.5,
			master_set_by_operator: true,
			channels: { 1: mixerChannel(), 2: mixerChannel(), 3: mixerChannel(), 4: mixerChannel() }
		},
		stems: {
			1: { ...stemsSnapshotNeutral(), vocal: { muted: true, solo: false, gain: 0.25 } },
			2: stemsSnapshotNeutral(),
			3: stemsSnapshotNeutral(),
			4: stemsSnapshotNeutral()
		}
	};
}

const STEM_CONTROLS_NEUTRAL = {
	vocal: { muted: false, solo: false, gain: 0.5 },
	instrumental: { muted: false, solo: false, gain: 0.5 },
	drums: { muted: false, solo: false, gain: 0.5 }
};

const STEM_READY = {
	status: 'ready',
	error: null,
	available_controls: ['vocal', 'instrumental', 'drums'],
	controls: { ...STEM_CONTROLS_NEUTRAL }
};

function emptyDeckStemState(status) {
	return {
		status,
		error: null,
		available_controls: [],
		controls: { ...STEM_CONTROLS_NEUTRAL }
	};
}

function emptyDecks() {
	return {
		1: {
			stable_id: null,
			position_ms: 0,
			playing: false,
			pitch: 1,
			pitch_range: 8,
			quantize_enabled: true,
			beat_sync_enabled: false,
			master_tempo_enabled: true,
			key_sync_enabled: false,
			load_generation: 1,
			stems: emptyDeckStemState('unavailable')
		},
		2: {
			stable_id: null,
			position_ms: 0,
			playing: false,
			pitch: 1,
			pitch_range: 8,
			quantize_enabled: true,
			beat_sync_enabled: false,
			master_tempo_enabled: true,
			key_sync_enabled: false,
			load_generation: 1,
			stems: emptyDeckStemState('unavailable')
		},
		3: {
			stable_id: null,
			position_ms: 0,
			playing: false,
			pitch: 1,
			pitch_range: 8,
			quantize_enabled: true,
			beat_sync_enabled: false,
			master_tempo_enabled: true,
			key_sync_enabled: false,
			load_generation: 1,
			stems: emptyDeckStemState('unavailable')
		},
		4: {
			stable_id: null,
			position_ms: 0,
			playing: false,
			pitch: 1,
			pitch_range: 8,
			quantize_enabled: true,
			beat_sync_enabled: false,
			master_tempo_enabled: true,
			key_sync_enabled: false,
			load_generation: 1,
			stems: emptyDeckStemState('unavailable')
		}
	};
}

function buildState() {
	return {
		browser: { active_playlist: null },
		master_deck: null,
		mixer: {
			crossfader: 0.5,
			master: 0.5,
			channels: { 1: mixerChannel(), 2: mixerChannel(), 3: mixerChannel(), 4: mixerChannel() }
		},
		decks: emptyDecks()
	};
}

test('restoring a session whose stem deck returns to loading mid-replay dispatches every stem command with no session-restore failed toast', async () => {
	stores.toasts.length = 0;
	const raw = snapshot.serializePerformanceSession(snapshotInput());
	const storage = new Map([[snapshot.PERFORMANCE_SESSION_STORAGE_KEY, raw]]);
	const state = buildState();
	const dispatched = [];
	let settled = 0;
	let firstStemOnDeck1 = true;

	globalThis.window = {};
	try {
		const dispose = session.installPerformanceSessionRestore({
			location: LOCATION,
			storage: {
				getItem: (k) => storage.get(k) ?? null,
				setItem: () => {}
			},
			replaceState: () => {},
			dispatch: async (command) => {
				dispatched.push(command);
				if (command.type === 'load') {
					state.decks[command.deck].stable_id = command.stable_id;
					state.decks[command.deck].stems = { ...STEM_READY };
					return state;
				}
				if (command.type === 'seek') return state;
				if (
					command.type === 'stem_mute' ||
					command.type === 'stem_solo' ||
					command.type === 'stem_gain'
				) {
					if (command.deck !== 1) return state;
					if (firstStemOnDeck1) {
						firstStemOnDeck1 = false;
						state.decks[1].stems = {
							status: 'loading',
							error: null,
							available_controls: [],
							controls: { ...STEM_CONTROLS_NEUTRAL }
						};
						setTimeout(() => {
							state.decks[1].stems = { ...STEM_READY, controls: { ...STEM_CONTROLS_NEUTRAL } };
						}, 5);
					} else if (state.decks[1].stems.status === 'loading') {
						throw new Error('deck 1 stems are loading: no aligned artifact');
					}
					return state;
				}
				return state;
			},
			query: () => state,
			document: { hidden: false, addEventListener: () => {}, removeEventListener: () => {} },
			window: { addEventListener: () => {}, removeEventListener: () => {} },
			setInterval: () => 1,
			clearInterval: () => {},
			commandSession: () => 1,
			operatorMaster: () => null,
			onDeckRestoreSettled: () => {
				settled += 1;
			}
		});
		await withinRealMs(
			new Promise((resolve) => {
				const id = setInterval(() => {
					if (settled > 0) {
						clearInterval(id);
						resolve();
					}
				}, 25);
			}),
			WITHIN_MS,
			'onDeckRestoreSettled'
		);
		dispose();
	} finally {
		delete globalThis.window;
	}

	const restoreFailedToasts = stores.toasts.filter((t) =>
		/could not be restored/.test(String(t.message))
	);
	assert.deepEqual(
		restoreFailedToasts.map((t) => t.message),
		[],
		'must not toast session restore failure for transient stems loading'
	);
	assert.equal(state.decks[1].stable_id, TRACK_ID);
	const stemForDeck1 = dispatched.filter(
		(c) =>
			(c.type === 'stem_mute' || c.type === 'stem_solo' || c.type === 'stem_gain') && c.deck === 1
	);
	const vocalStem = stemForDeck1.filter((c) => c.stem === 'vocal');
	assert.deepEqual(vocalStem, [
		{ type: 'stem_mute', deck: 1, stem: 'vocal', muted: true },
		{ type: 'stem_solo', deck: 1, stem: 'vocal', solo: false },
		{ type: 'stem_gain', deck: 1, stem: 'vocal', value: 0.25 }
	]);
	assert.equal(
		stemForDeck1.length,
		9,
		'snapshot replays vocal, instrumental and drums (three IPC commands each)'
	);
});

test('a genuine stems error during restore still raises a named deck restore toast', async () => {
	stores.toasts.length = 0;
	const raw = snapshot.serializePerformanceSession(snapshotInput());
	const storage = new Map([[snapshot.PERFORMANCE_SESSION_STORAGE_KEY, raw]]);
	const state = buildState();
	const dispatched = [];
	let settled = 0;

	globalThis.window = {};
	try {
		const dispose = session.installPerformanceSessionRestore({
			location: LOCATION,
			storage: {
				getItem: (k) => storage.get(k) ?? null,
				setItem: () => {}
			},
			replaceState: () => {},
			dispatch: async (command) => {
				dispatched.push(command);
				if (command.type === 'load') {
					state.decks[command.deck].stable_id = command.stable_id;
					state.decks[command.deck].stems = {
						status: 'error',
						error: 'decode failed',
						available_controls: [],
						controls: { ...STEM_CONTROLS_NEUTRAL }
					};
					return state;
				}
				if (command.type === 'seek') return state;
				return state;
			},
			query: () => state,
			document: { hidden: false, addEventListener: () => {}, removeEventListener: () => {} },
			window: { addEventListener: () => {}, removeEventListener: () => {} },
			setInterval: () => 1,
			clearInterval: () => {},
			commandSession: () => 1,
			operatorMaster: () => null,
			onDeckRestoreSettled: () => {
				settled += 1;
			}
		});
		await withinRealMs(
			new Promise((resolve) => {
				const id = setInterval(() => {
					if (settled > 0) {
						clearInterval(id);
						resolve();
					}
				}, 25);
			}),
			WITHIN_MS,
			'onDeckRestoreSettled'
		);
		dispose();
	} finally {
		delete globalThis.window;
	}

	const restoreFailedToasts = stores.toasts.filter((t) =>
		/could not be restored/.test(String(t.message))
	);
	assert.ok(
		restoreFailedToasts.some((t) => String(t.message).startsWith('Deck 1 could not be restored')),
		`expected a named deck 1 restore toast, got ${restoreFailedToasts.map((t) => t.message).join('; ')}`
	);
	const stemForDeck1 = dispatched.filter(
		(c) =>
			(c.type === 'stem_mute' || c.type === 'stem_solo' || c.type === 'stem_gain') && c.deck === 1
	);
	assert.deepEqual(stemForDeck1, [], 'no stem commands when stems are already error');
});
