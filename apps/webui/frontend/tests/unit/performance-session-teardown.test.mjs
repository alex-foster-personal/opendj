// A session snapshot must describe the route the operator was using, never
// the route being torn down. On demon-llama (Thu 1 Oct 2026) a preview
// restart remounted /performance inside the running page; a second session
// restore at 05:49:53Z read a snapshot whose master was 0 and dispatched
// master_volume 0, so the room came back silent with the decks stopped. 0 is
// exactly what the route teardown's hard mute writes into the mixer.
//
// requirement: SESSION-TEARDOWN-01
// [if] the route session that owns a snapshot writer ends and the writer fires afterwards [then] nothing is persisted
// [if] a session restore is installed after its route already unmounted [then] it never writes a snapshot
// [if] a snapshot holds master 0 that no operator chose [then] restore does not replay it
// [if] the live master was zeroed by a safety mute after the operator set a level [then] the operator's level is persisted
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let session;
let snapshotModule;
let ipc;

before(async () => {
	({ session, snapshot: snapshotModule, ipc } = await loadTypeScriptModule(
		'tests/unit/fixtures/performance-session-restore-entry.ts'
	));
});

function channel() {
	return { trim: 0.5, eq_high: 0.5, eq_mid: 0.5, eq_low: 0.5, filter: 0.5, fader: 1, assign: 'THRU' };
}

function deck(stable_id, playing) {
	return {
		stable_id,
		playing,
		position_ms: stable_id === null ? 0 : 42_000,
		pitch: 1,
		pitch_range: 8,
		quantize_enabled: true,
		beat_sync_enabled: true,
		master_tempo_enabled: true,
		key_sync_enabled: false,
		stems: {
			available_controls: ['vocal', 'instrumental', 'drums'],
			controls: {
				vocal: { muted: false, solo: false, gain: 0.5 },
				instrumental: { muted: false, solo: false, gain: 0.5 },
				drums: { muted: false, solo: false, gain: 0.5 }
			}
		}
	};
}

/** A live /performance state: one deck playing, master at the operator's 0.16. */
function liveState() {
	return {
		browser: { active_playlist: null },
		mixer: { crossfader: 0.5, master: 0.16, channels: { 1: channel(), 2: channel(), 3: channel(), 4: channel() } },
		decks: { 1: deck('sid-a', true), 2: deck(null, false), 3: deck(null, false), 4: deck(null, false) }
	};
}

/** A freshly loaded page: nothing on any deck yet, so a restore runs (RESCUE-07
 * skips a restore that would replay over decks already loaded). */
function emptyState() {
	const state = liveState();
	state.decks = { 1: deck(null, false), 2: deck(null, false), 3: deck(null, false), 4: deck(null, false) };
	return state;
}

function harness(state, initial = null) {
	const store = new Map();
	if (initial !== null) store.set(snapshotModule.PERFORMANCE_SESSION_STORAGE_KEY, initial);
	const listeners = new Map();
	const dispatched = [];
	return {
		store,
		listeners,
		dispatched,
		persisted: () => {
			const raw = store.get(snapshotModule.PERFORMANCE_SESSION_STORAGE_KEY);
			return raw === undefined ? null : snapshotModule.parsePerformanceSession(raw);
		},
		options: {
			location: { pathname: '/performance', search: '?d1=sid-a', href: 'http://127.0.0.1/performance?d1=sid-a' },
			storage: {
				getItem: (key) => (store.has(key) ? store.get(key) : null),
				setItem: (key, value) => store.set(key, value)
			},
			replaceState: () => {},
			dispatch: async (command) => {
				dispatched.push(command);
				return state;
			},
			query: () => state,
			document: {
				hidden: false,
				addEventListener: (type, handler) => listeners.set(`document:${type}`, handler),
				removeEventListener: (type) => listeners.delete(`document:${type}`)
			},
			window: {
				addEventListener: (type, handler) => listeners.set(`window:${type}`, handler),
				removeEventListener: (type) => listeners.delete(`window:${type}`)
			},
			setInterval: () => 1,
			clearInterval: () => {}
		}
	};
}

async function settle() {
	for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

function serializedSnapshot(master, extra = {}) {
	const state = liveState();
	const raw = JSON.parse(session.buildPerformanceSessionSnapshot(state, 1_700_000_000_000, null));
	raw.mixer.master = master;
	delete raw.mixer.master_set_by_operator;
	Object.assign(raw.mixer, extra);
	return JSON.stringify(raw);
}

test('a snapshot writer that outlives its route never persists the torn-down state', async () => {
	globalThis.window = {};
	const state = liveState();
	const h = harness(state);
	const uninstallIpc = ipc.installPerformanceBrowserIpc();
	let ipcInstalled = true;
	try {
		session.installPerformanceSessionRestore({ ...h.options, skipDeckRestore: true });
		await settle();
		assert.equal(h.persisted()?.mixer.master, 0.16, 'the live route persists the operator level');
		// Route teardown: the command session ends, then the hard mute and the
		// graceful stop land on the shared mixer and decks.
		uninstallIpc();
		ipcInstalled = false;
		state.mixer.master = 0;
		state.decks[1].playing = false;
		h.listeners.get('window:pagehide')();
		assert.equal(h.persisted()?.mixer.master, 0.16, 'a teardown-time snapshot must never be persisted');
	} finally {
		if (ipcInstalled) uninstallIpc();
		delete globalThis.window;
	}
});

test('an old writer stays silent after the route remounts in the same page', async () => {
	globalThis.window = {};
	const state = liveState();
	const h = harness(state);
	const uninstallFirst = ipc.installPerformanceBrowserIpc();
	let uninstallSecond = null;
	try {
		session.installPerformanceSessionRestore({ ...h.options, skipDeckRestore: true });
		await settle();
		const oldPageHide = h.listeners.get('window:pagehide');
		// The preview reload remounts /performance in the running page: the old
		// session ends and a new one starts before the old writer fires.
		uninstallFirst();
		delete globalThis.window.musicDjToolsPerformance;
		uninstallSecond = ipc.installPerformanceBrowserIpc();
		state.mixer.master = 0;
		oldPageHide();
		assert.equal(h.persisted()?.mixer.master, 0.16, 'only the new mount may write the next snapshot');
	} finally {
		uninstallSecond?.();
		delete globalThis.window;
	}
});

test('a session restore installed after its route already unmounted never writes', async () => {
	globalThis.window = {};
	const state = liveState();
	state.mixer.master = 0;
	const h = harness(state, serializedSnapshot(0.16, { master_set_by_operator: true }));
	const before = h.store.get(snapshotModule.PERFORMANCE_SESSION_STORAGE_KEY);
	try {
		// No command session: the route's IPC was already uninstalled.
		session.installPerformanceSessionRestore({ ...h.options, skipDeckRestore: true });
		await settle();
		assert.equal(
			h.store.get(snapshotModule.PERFORMANCE_SESSION_STORAGE_KEY),
			before,
			'an orphaned restore must leave the last good snapshot alone'
		);
	} finally {
		delete globalThis.window;
	}
});

test('restore does not replay a master of 0 that no operator chose', async () => {
	globalThis.window = {};
	const h = harness(emptyState(), serializedSnapshot(0));
	try {
		const dispose = session.installPerformanceSessionRestore(h.options);
		await settle();
		dispose();
		assert.deepEqual(
			h.dispatched.filter((command) => command.type === 'master_volume'),
			[],
			'an unattributed 0 is a captured safety mute, not a level'
		);
	} finally {
		delete globalThis.window;
	}
});

test('restore replays a master of 0 the operator set, and any non-zero level (control)', async () => {
	globalThis.window = {};
	try {
		for (const [raw, expected] of [
			[serializedSnapshot(0, { master_set_by_operator: true }), 0],
			[serializedSnapshot(0.16), 0.16]
		]) {
			const h = harness(emptyState(), raw);
			const dispose = session.installPerformanceSessionRestore(h.options);
			await settle();
			dispose();
			assert.deepEqual(
				h.dispatched.filter((command) => command.type === 'master_volume'),
				[{ type: 'master_volume', value: expected }]
			);
		}
	} finally {
		delete globalThis.window;
	}
});

test('a safety mute after the operator set a level persists the operator level', async () => {
	globalThis.window = {};
	const state = liveState();
	state.mixer.master = 0;
	const h = harness(state);
	const uninstallIpc = ipc.installPerformanceBrowserIpc();
	try {
		session.installPerformanceSessionRestore({
			...h.options,
			skipDeckRestore: true,
			operatorMaster: () => 0.16
		});
		await settle();
		const persisted = h.persisted();
		assert.equal(persisted?.mixer.master, 0.16);
		assert.equal(persisted?.mixer.master_set_by_operator, true);
	} finally {
		uninstallIpc();
		delete globalThis.window;
	}
});

test('a master_volume command executed by the IPC is recorded as the operator level', async () => {
	globalThis.window = {};
	const uninstallIpc = ipc.installPerformanceBrowserIpc();
	try {
		assert.equal(typeof ipc.operatorMasterVolume, 'function', 'the IPC exposes the operator level');
		await window.musicDjToolsPerformance.dispatch({ type: 'master_volume', value: 0.16 });
		assert.equal(ipc.operatorMasterVolume(), 0.16);
	} finally {
		uninstallIpc();
		delete globalThis.window;
	}
});
