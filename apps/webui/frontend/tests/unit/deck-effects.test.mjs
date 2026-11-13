/**
 * Issue #10282 / DECKUX-11852: deck-state effects run from deck-effects.svelte.ts
 * for all four decks with no Deck panel mounted.
 *
 * - [if] performance raises a deck error [then] noteDeckError runs and mints an id
 * - [if] an expected banner is shown with stems loading [then] a dismiss timer arms
 * - [if] lyrics prefs are on and a track loads [then] lyrics warm-up starts
 * - [if] a deck shows NO STEMS [then] the separation port loads or records loadError
 * - [if] library_jobs names the track [then] stem_load dispatches once via the refresh subscription
 * - [if] startDeckEffects is called twice [then] subscriptions do not double-fire
 * - [if] stop runs [then] subscriptions and effects tear down
 * - [if] Deck.svelte is read as text [then] moved effect calls live only in the store
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, afterEach, before, beforeEach, mock, test } from 'node:test';

import { loadRuneModule } from './load-rune-module.mjs';

const NO_BUNDLE = 'no bundle on this machine and none indexed in the cloud';
const WS_URL = 'ws://deck-effects.example.test/api/v1/events';

const ENTRY = `
import { flushSync } from 'svelte';
import { startDeckEffects, deckSeparation, deckSeparateId } from '$lib/rb/deck-effects.svelte';
import { deckErrorIds } from '$lib/rb/deck-error-id.svelte';
import { deckStates } from '$lib/rb/audio-engine.svelte';
import {
	performanceCommandStatus,
	installPerformanceBrowserIpc,
	claimPerformanceMount
} from '$lib/rb/performance-ipc.svelte';
import { uiPrefs } from '$lib/rb/prefs.svelte';
import { lyricEntry } from '$lib/lyrics/lyrics-cache.svelte';
import { unavailableStemDeckState } from '$lib/rb/stem-graph';
import { initRustMode, rustMode } from '$lib/audio-engine/rust-mode.svelte';
import { stemSeparation } from '$lib/rb/stem-separation-port';

export {
	startDeckEffects,
	deckSeparation,
	deckSeparateId,
	deckErrorIds,
	deckStates,
	performanceCommandStatus,
	uiPrefs,
	lyricEntry,
	installPerformanceBrowserIpc,
	claimPerformanceMount,
	unavailableStemDeckState,
	initRustMode,
	rustMode,
	stemSeparation,
	flushSync
};
// Re-exported by name so the unit-seam check can follow _resetForTests to events-bus.
export { connect, disconnect, _resetForTests } from '$lib/api/events-bus';
`;

class FakeSocket {
	constructor(url) {
		this.url = url;
		this.onopen = null;
		this.onmessage = null;
		this.onclose = null;
		this.onerror = null;
		this.closedByClient = false;
	}

	open() {
		this.onopen?.({});
	}

	deliver(topic, seq, payload) {
		this.onmessage?.({
			data: JSON.stringify({ topic, seq, ts: '2026-11-10T08:00:00.000Z', payload })
		});
	}

	hello({ contractRev = 'rev-1', engineVersion = '1.0.0', seqStart = 0 } = {}) {
		this.deliver('hello', seqStart, {
			contract_rev: contractRev,
			engine_version: engineVersion,
			seq_start: seqStart,
			topics: []
		});
	}

	close() {
		this.closedByClient = true;
	}

	send() {}
}

function _captureConsole() {
	const calls = { info: [], warn: [], error: [] };
	const real = { info: console.info, warn: console.warn, error: console.error };
	console.info = (...args) => calls.info.push(args.join(' '));
	console.warn = (...args) => calls.warn.push(args.join(' '));
	console.error = (...args) => calls.error.push(args.join(' '));
	return {
		calls,
		restore: () => {
			console.info = real.info;
			console.warn = real.warn;
			console.error = real.error;
		}
	};
}

/** @type {Record<string, unknown>} */
let mod;
let sockets;
let originalFetch;
let originalWindow;
/** @type {(() => void) | null} */
let uninstallIpc = null;

function _stubWindow() {
	const store = new Map();
	return /** @type {Window & typeof globalThis} */ ({
		localStorage: {
			getItem: (key) => store.get(key) ?? null,
			setItem: (key, value) => store.set(key, String(value)),
			removeItem: (key) => store.delete(key)
		},
		sessionStorage: {
			getItem: () => null,
			setItem: () => {},
			removeItem: () => {}
		}
	});
}

before(async () => {
	originalFetch = globalThis.fetch;
	originalWindow = globalThis.window;
	globalThis.window = _stubWindow();
	mod = await loadRuneModule(ENTRY);
});

beforeEach(() => {
	sockets = [];
	globalThis.fetch = originalFetch;
	activeDeckEffectsStop?.();
	activeDeckEffectsStop = null;
	mod._resetForTests();
	mod.initRustMode();
	mod.rustMode.enabled = false;
	mod.claimPerformanceMount();
	uninstallIpc = mod.installPerformanceBrowserIpc();
	for (const deck of [1, 2, 3, 4]) {
		mod.performanceCommandStatus.deck_errors[deck] = null;
		mod.deckStates[deck].stable_id = null;
		mod.deckStates[deck].stems = mod.unavailableStemDeckState(null);
	}
	mod.uiPrefs.lyrics_deck_line = false;
	mod.uiPrefs.lyrics_global = false;
});

let activeDeckEffectsStop = null;

afterEach(() => {
	globalThis.fetch = originalFetch;
	mock.reset();
	activeDeckEffectsStop?.();
	activeDeckEffectsStop = null;
	uninstallIpc?.();
	uninstallIpc = null;
	mod.disconnect();
	mod._resetForTests();
	for (const deck of [1, 2, 3, 4]) {
		mod.performanceCommandStatus.deck_errors[deck] = null;
	}
});

after(() => {
	globalThis.fetch = originalFetch;
	globalThis.window = originalWindow;
});

function connectBus() {
	mod.connect(WS_URL, {
		socketFactory: (url) => {
			const socket = new FakeSocket(url);
			sockets.push(socket);
			return socket;
		}
	});
	const socket = sockets.at(-1);
	socket.open();
	socket.hello();
	return socket;
}

function noStemsDeck(deck, stableId) {
	mod.deckStates[deck].stable_id = stableId;
	mod.deckStates[deck].stems = mod.unavailableStemDeckState(NO_BUNDLE);
}

test('deck error is logged with no Deck mounted', () => {
	activeDeckEffectsStop = mod.startDeckEffects();
	const capture = _captureConsole();
	try {
		mod.performanceCommandStatus.deck_errors[1] = 'boom';
		mod.flushSync();
	} finally {
		capture.restore();
		activeDeckEffectsStop?.();
		activeDeckEffectsStop = null;
	}
	assert.match(mod.deckErrorIds[1], /^t-\d+$/);
});

test('expected banner auto-dismiss arms one timer', () => {
	const delays = [];
	const realSetTimeout = globalThis.setTimeout;
	globalThis.setTimeout = (fn, ms) => {
		delays.push(ms);
		return realSetTimeout(() => {}, 0);
	};
	activeDeckEffectsStop = mod.startDeckEffects();
	try {
		const msg = 'deck 1 stems are loading: no aligned artifact';
		mod.performanceCommandStatus.deck_errors[1] = msg;
		mod.deckStates[1].stems.status = 'loading';
		mod.flushSync();
		const expected = delays.filter((ms) => ms === 5000);
		assert.equal(expected.length, 1);
		const beforeUnrelated = delays.length;
		mod.deckStates[1].position_ms = 1;
		mod.flushSync();
		assert.equal(
			delays.length,
			beforeUnrelated,
			'unrelated deck writes must not restart the dismiss timer'
		);
	} finally {
		globalThis.setTimeout = realSetTimeout;
		activeDeckEffectsStop?.();
		activeDeckEffectsStop = null;
	}
});

test('lyrics load on track load when prefs are on', async () => {
	globalThis.fetch = () =>
		Promise.resolve(
			new Response('[]', {
				status: 200,
				headers: { 'content-type': 'application/json' }
			})
		);
	mod.uiPrefs.lyrics_deck_line = true;
	mod.uiPrefs.lyrics_global = true;
	activeDeckEffectsStop = mod.startDeckEffects();
	mod.deckStates[1].stable_id = 'deck-effects-a';
	mod.flushSync();
	const entry = mod.lyricEntry('deck-effects-a');
	assert.notEqual(entry, null, 'lyrics warm-up did not start');
	assert.ok(entry.state === 'loading' || entry.state === 'error');
	mod.uiPrefs.lyrics_deck_line = false;
	mod.deckStates[1].stable_id = 'deck-effects-b';
	mod.flushSync();
	assert.equal(mod.lyricEntry('deck-effects-b'), null, 'with deck line off nothing loads');
	activeDeckEffectsStop?.();
	activeDeckEffectsStop = null;
});

test('stem separation load runs for NO STEMS with no Deck mounted', async () => {
	activeDeckEffectsStop = mod.startDeckEffects();
	noStemsDeck(2, 'deck-effects-stem');
	mod.flushSync();
	try {
		await mod.stemSeparation();
	} catch {
		// The store chunk may fail under the harness; the deck effect still arms the load.
	}
	for (let i = 0; i < 8; i += 1) await new Promise((resolve) => setImmediate(resolve));
	const slot = mod.deckSeparation(2);
	assert.ok(
		slot.port !== null || slot.loadError !== null,
		'neither port nor loadError was set by the separation effect'
	);
	activeDeckEffectsStop?.();
	activeDeckEffectsStop = null;
});

test('stems refresh runs stem_load once per library_jobs event', async () => {
	let retryCalls = 0;
	const engine = mod.deckStates[1];
	const original = engine.stems;
	globalThis.fetch = async () => {
		retryCalls += 1;
		return new Response(JSON.stringify({ stems: false }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};
	noStemsDeck(1, 'trk-refresh');
	connectBus();
	activeDeckEffectsStop = mod.startDeckEffects();
	mod.flushSync();
	const socket = sockets.at(-1);
	const before = retryCalls;
	socket.deliver('library.changed', 1, { kind: 'library_jobs', ids: ['trk-refresh'] });
	mod.flushSync();
	await new Promise((resolve) => setImmediate(resolve));
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(
		retryCalls - before,
		1,
		'stem_load must run exactly once (stem_load has no deckCommandDispatchCount)'
	);
	engine.stems = original;
	activeDeckEffectsStop?.();
	activeDeckEffectsStop = null;
});

test('startDeckEffects is idempotent', () => {
	const a = mod.startDeckEffects();
	const b = mod.startDeckEffects();
	mod.performanceCommandStatus.deck_errors[1] = 'idem-a';
	mod.flushSync();
	const first = mod.deckErrorIds[1];
	a();
	mod.performanceCommandStatus.deck_errors[1] = 'idem-b';
	mod.flushSync();
	assert.notEqual(mod.deckErrorIds[1], first, 'one refcount release must not stop the shared root');
	b();
	const frozen = mod.deckErrorIds[1];
	mod.performanceCommandStatus.deck_errors[1] = 'idem-c';
	mod.flushSync();
	assert.equal(mod.deckErrorIds[1], frozen, 'the last stop must tear the root down');
	a();
	a();
});

test('stop removes every subscription', async () => {
	let retryCalls = 0;
	globalThis.fetch = async () => {
		retryCalls += 1;
		return new Response(JSON.stringify({ stems: false }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};
	activeDeckEffectsStop = mod.startDeckEffects();
	connectBus();
	noStemsDeck(1, 'trk-stop');
	mod.flushSync();
	activeDeckEffectsStop?.();
	activeDeckEffectsStop = null;
	const before = retryCalls;
	sockets.at(-1).deliver('library.changed', 1, { kind: 'library_jobs', ids: ['trk-stop'] });
	mod.flushSync();
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(retryCalls, before);
	mod.uiPrefs.lyrics_deck_line = true;
	mod.uiPrefs.lyrics_global = true;
	mod.deckStates[1].stable_id = 'trk-stop-2';
	mod.flushSync();
	assert.equal(mod.lyricEntry('trk-stop-2'), null);
});

test('Deck.svelte keeps only layout and pause effects; store owns deck-state behaviours', () => {
	const deck = readFileSync(
		new URL('../../src/lib/components/rb/Deck.svelte', import.meta.url),
		'utf8'
	);
	const store = readFileSync(
		new URL('../../src/lib/rb/deck-effects.svelte.ts', import.meta.url),
		'utf8'
	);
	const moved = [
		'noteDeckError\\(',
		'loadLyrics\\(',
		'subscribeLoadedDeckStemsRefresh\\(',
		'stemSeparation\\(',
		'\\.ensureChecked\\(',
		'new SeparateEndProbe\\(',
		'\\.noteNoStems\\(',
		'deckBannerDismissMs\\('
	];
	for (const pattern of moved) {
		assert.doesNotMatch(deck, new RegExp(pattern));
		assert.match(store, new RegExp(pattern));
	}
	assert.doesNotMatch(deck, /stemsSettled/);
	assert.match(store, /_stemsSettled\(/);
	const effectCount = (deck.match(/\$effect\(/g) ?? []).length;
	assert.equal(
		effectCount,
		3,
		'Deck must keep exactly three $effect blocks (pause anchor, pause dim, ResizeObserver)'
	);
	assert.match(deck, /ResizeObserver/);
	assert.match(deck, /pausedSinceMs/);
});
