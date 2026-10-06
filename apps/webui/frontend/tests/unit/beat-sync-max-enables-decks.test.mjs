/**
 * Beat Sync Max turns Beat Sync ON (DECKUX-37).
 *
 * the maintainer, Tue 6 Oct 2026, /performance: "beatsyncMax is on but each deck is not
 * turning on beatsync - why?" The preview mirror had all four decks at
 * sync {mode: "bar", enabled: false}: Max only forced BAR on decks already
 * synced and never enabled one.
 *
 * Regression lines:
 * - if Max ON stops enabling loaded decks, "three loaded decks" fails
 * - if Max OFF starts unsyncing (the overshoot), "Max off changes no deck" fails
 * - if the load path stops calling the enabler, "a load under Max" fails
 * - if a re-sent Max ON re-enables a deck the DJ turned off, "sticks" fails
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://beat-sync-max.example.test';
const DECKS = [1, 2, 3, 4];
let m;

before(async () => {
	m = await loadTypeScriptModule('tests/unit/fixtures/beat-sync-max-enable-entry.ts', {
		viteApiBase: API_BASE
	});
});

function _view(loaded, enabled) {
	return { stable_id: loaded ? 'track' : null, beat_sync_enabled: enabled };
}

function _beatSyncCommandCount() {
	return m.queryPerformanceState().history.filter((event) => event.type === 'beat_sync').length;
}

async function _settle() {
	for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
}

/** Real prefs + real performance IPC, with storage and the ui-prefs PUT stubbed. */
async function _withSession(decks, run) {
	const store = new Map();
	globalThis.window = {
		localStorage: { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) }
	};
	const originalFetch = globalThis.fetch;
	globalThis.fetch = async () => Response.json({});
	const uninstall = m.installPerformanceBrowserIpc();
	try {
		m.uiPrefs.beat_sync_max = decks.max;
		for (const id of DECKS) {
			const [loaded, enabled] = decks[id];
			Object.assign(m.deckStates[id], {
				stable_id: loaded ? `track-${id}` : null,
				beat_sync_enabled: enabled,
				playing: false,
				anlz: null
			});
		}
		await run();
	} finally {
		uninstall();
		globalThis.fetch = originalFetch;
		delete globalThis.window;
	}
}

function _syncFlags() {
	return DECKS.map((id) => m.deckStates[id].beat_sync_enabled);
}

test('[if] Max goes on with three loaded decks [then] three beat_sync enables run, empty deck stays off', async () => {
	await _withSession(
		{ max: false, 1: [true, false], 2: [true, false], 3: [true, false], 4: [false, false] },
		async () => {
			const before = _beatSyncCommandCount();
			m.setBeatSyncMax(true);
			await _settle();
			assert.deepEqual(_syncFlags(), [true, true, true, false]);
			assert.equal(_beatSyncCommandCount() - before, 3, 'one deck SYNC command per loaded deck');
		}
	);
});

test('[if] the DJ turns one deck off under Max [then] it sticks until that deck loads again', async () => {
	await _withSession(
		{ max: false, 1: [true, false], 2: [true, false], 3: [true, false], 4: [false, false] },
		async () => {
			m.setBeatSyncMax(true);
			await _settle();
			await m.dispatchPerformanceCommand({ type: 'beat_sync', deck: 2, enabled: false });
			m.setBeatSyncMax(true);
			await _settle();
			assert.equal(m.deckStates[2].beat_sync_enabled, false, 'a re-sent Max ON is not an edge');
			await m.enableBeatSyncAfterLoad(2);
			assert.equal(m.deckStates[2].beat_sync_enabled, true, 'the next load re-enables it');
		}
	);
});

test('[if] Max goes off [then] no deck changes and no beat_sync command runs', async () => {
	await _withSession(
		{ max: true, 1: [true, true], 2: [true, false], 3: [true, true], 4: [false, false] },
		async () => {
			const before = _beatSyncCommandCount();
			m.setBeatSyncMax(false);
			await _settle();
			assert.deepEqual(_syncFlags(), [true, false, true, false]);
			assert.equal(_beatSyncCommandCount(), before);
		}
	);
});

test('[if] a deck loads under Max [then] that deck syncs; Max off or already on sends nothing', async () => {
	await _withSession(
		{ max: true, 1: [true, false], 2: [false, false], 3: [true, true], 4: [false, false] },
		async () => {
			const before = _beatSyncCommandCount();
			await m.enableBeatSyncAfterLoad(1);
			await m.enableBeatSyncAfterLoad(3);
			assert.equal(m.deckStates[1].beat_sync_enabled, true);
			assert.equal(_beatSyncCommandCount() - before, 1, 'an already-synced deck gets no command');
			m.uiPrefs.beat_sync_max = false;
			m.deckStates[1].beat_sync_enabled = false;
			await m.enableBeatSyncAfterLoad(1);
			assert.equal(m.deckStates[1].beat_sync_enabled, false, 'no Max, no enable');
		}
	);
});

test('[if] the policy is asked [then] only the off-to-on edge and a Max load enable decks', () => {
	const decks = { 1: _view(true, false), 2: _view(true, true), 3: _view(false, false), 4: _view(true, false) };
	const enable = (deck) => ({ type: 'beat_sync', deck, enabled: true });
	assert.deepEqual(m.beatSyncMaxToggleCommands(false, true, decks), [enable(1), enable(4)]);
	assert.deepEqual(m.beatSyncMaxToggleCommands(true, false, decks), []);
	assert.deepEqual(m.beatSyncMaxToggleCommands(true, true, decks), []);
	assert.deepEqual(m.beatSyncMaxToggleCommands(false, false, decks), []);
	assert.deepEqual(m.beatSyncMaxLoadCommand(true, 1, decks[1]), enable(1));
	assert.equal(m.beatSyncMaxLoadCommand(true, 2, decks[2]), null);
	assert.equal(m.beatSyncMaxLoadCommand(true, 3, decks[3]), null);
	assert.equal(m.beatSyncMaxLoadCommand(false, 1, decks[1]), null);
	assert.throws(() => m.beatSyncMaxToggleCommands(0, true, decks), TypeError);
});

test('[if] the load path or the setter is rewired [then] the wiring contract fails', () => {
	const ipc = readFrontendSource('src/lib/rb/performance-ipc.svelte.ts');
	assert.match(
		ipc,
		/if \(command\.type !== 'load'\) return settled;[\s\S]{0,300}await enableBeatSyncAfterLoad\(loadedDeck\);/,
		'a load must queue its deck enable after the load scope settles'
	);
	assert.match(ipc, /dispatch: \(command\) => runPerformanceCommandFromUi\(command\)/, 'deck SYNC button path');
	const prefs = readFrontendSource('src/lib/rb/prefs.svelte.ts');
	assert.match(prefs, /announceBeatSyncMaxChange\(previous, next\)/);
});
