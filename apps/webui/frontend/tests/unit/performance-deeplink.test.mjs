import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// requirement: DEEPLINK-01
// [if] the user opens /performance?playlist=<id> [then] the browser panel restores that collection playlist on load
// [if] decks are loaded and the user refreshes [then] each deck reloads its last stable_id and seeks to the last known position_ms within 1s of the pagehide snapshot
// [if] the user adjusts EQ, fader, or stem mute/solo [then] that lv3 state survives refresh via the snapshot without a replaceState or storage write on the audio thread
// [if] the deep-link code runs without a window/location [then] it guards the read and never throws

async function _loadDeeplink() {
	return loadTypeScriptModule('src/lib/rb/performance-deeplink.ts');
}

async function _loadSnapshot() {
	return loadTypeScriptModule('src/lib/rb/performance-session-snapshot.ts');
}

async function _loadSession() {
	return loadTypeScriptModule('src/lib/rb/performance-session.svelte.ts');
}

async function _loadContract() {
	return loadTypeScriptModule('src/lib/components/rb/browser/pane-contract.svelte.ts');
}

function _defaultChannel() {
	return {
		trim: 0.5,
		eq_high: 0.5,
		eq_mid: 0.5,
		eq_low: 0.5,
		filter: 0.5,
		fader: 1,
		assign: 'THRU'
	};
}

function _defaultDeck(overrides = {}) {
	return {
		stable_id: null,
		position_ms: 0,
		pitch: 1,
		pitch_range: 8,
		quantize_enabled: true,
		beat_sync_enabled: true,
		master_tempo_enabled: true,
		key_sync_enabled: false,
		...overrides
	};
}

function _defaultStems() {
	return {
		vocal: { muted: false, solo: false, gain: 0.5 },
		instrumental: { muted: false, solo: false, gain: 0.5 },
		drums: { muted: false, solo: false, gain: 0.5 }
	};
}

function _snapshotFixture() {
	return {
		captured_at_ms: 1_700_000_000_000,
		playlist_id: 'pl-abc',
		decks: {
			1: _defaultDeck({ stable_id: 'sid-a', position_ms: 12_345, pitch: 1.02 }),
			2: _defaultDeck(),
			3: _defaultDeck({ stable_id: 'sid-c', position_ms: 4_000 }),
			4: _defaultDeck()
		},
		mixer: {
			crossfader: 0.5,
			master: 0.8,
			channels: {
				1: { ..._defaultChannel(), eq_high: 0.2, eq_mid: 0.5, eq_low: 0.8, fader: 0.7 },
				2: _defaultChannel(),
				3: _defaultChannel(),
				4: _defaultChannel()
			}
		},
		stems: {
			1: { ..._defaultStems(), vocal: { muted: true, solo: false } },
			2: _defaultStems(),
			3: _defaultStems(),
			4: _defaultStems()
		}
	};
}

test('formatReplaceStateUrl emits a single ? for collection playlist', async () => {
	const deeplink = await _loadDeeplink();
	const url = new URL('http://127.0.0.1/performance');
	url.search = deeplink.writeLv1(url.searchParams, {
		source: 'collection',
		playlist_id: 'pl-abc'
	}).toString();
	assert.equal(deeplink.formatReplaceStateUrl(url), '/performance?playlist=pl-abc');
});

test('formatReplaceStateUrl emits a single ? for spotify playlist', async () => {
	const deeplink = await _loadDeeplink();
	const url = new URL('http://127.0.0.1/performance');
	url.search = deeplink.writeLv1(url.searchParams, {
		source: 'spotify',
		playlist_id: 'sp-1'
	}).toString();
	assert.equal(deeplink.formatReplaceStateUrl(url), '/performance?source=spotify&playlist=sp-1');
});

test('formatReplaceStateUrl emits a single ? for lv2 deck ids', async () => {
	const deeplink = await _loadDeeplink();
	const url = new URL('http://127.0.0.1/performance?playlist=pl-abc');
	url.search = deeplink.writeLv2Ids(url.searchParams, { 1: 'sid-a', 3: 'sid-c' }).toString();
	assert.equal(deeplink.formatReplaceStateUrl(url), '/performance?playlist=pl-abc&d1=sid-a&d3=sid-c');
});

test('6a812c8ac2e5: lv1 collection playlist round-trips without a source key', async () => {
	const deeplink = await _loadDeeplink();
	const lv1 = { source: 'collection', playlist_id: 'pl-abc' };
	const params = deeplink.writeLv1(new URLSearchParams('d1=keep-me'), lv1);
	assert.equal(params.get('source'), null);
	assert.equal(params.get('playlist'), 'pl-abc');
	assert.equal(params.get('d1'), 'keep-me');
	assert.deepEqual(deeplink.parseLv1(params), lv1);
	assert.equal(deeplink.applyToLocation('?playlist=pl-abc'), '?playlist=pl-abc');
});

test('lv1 collection playlist=all round-trips', async () => {
	const deeplink = await _loadDeeplink();
	const lv1 = { source: 'collection', playlist_id: 'all' };
	const params = deeplink.writeLv1(new URLSearchParams(), lv1);
	assert.equal(params.get('playlist'), 'all');
	assert.deepEqual(deeplink.parseLv1(params), lv1);
});

test('lv1 spotify keeps source=spotify', async () => {
	const deeplink = await _loadDeeplink();
	const lv1 = { source: 'spotify', playlist_id: 'sp-1' };
	const params = deeplink.writeLv1(new URLSearchParams(), lv1);
	assert.equal(params.get('source'), 'spotify');
	assert.equal(params.get('playlist'), 'sp-1');
	assert.deepEqual(deeplink.parseLv1(params), lv1);
});

test('lv1 empty playlist query parses as null', async () => {
	const deeplink = await _loadDeeplink();
	assert.deepEqual(deeplink.parseLv1('?playlist='), {
		source: 'collection',
		playlist_id: null
	});
	assert.deepEqual(deeplink.parseLv1(''), { source: 'collection', playlist_id: null });
});

test('6a812c8ac2e5: lv2 deck ids round-trip and omit empty decks', async () => {
	const deeplink = await _loadDeeplink();
	const ids = { 1: 'sid-a', 3: 'sid-c' };
	const params = deeplink.writeLv2Ids(new URLSearchParams(), ids);
	assert.equal(params.get('d1'), 'sid-a');
	assert.equal(params.get('d2'), null);
	assert.equal(params.get('d3'), 'sid-c');
	assert.deepEqual(deeplink.parseLv2Ids(params), ids);
});

test('6a812c8ac2e5: performance session snapshot round-trips lv3 fixture state', async () => {
	const snapshot = await _loadSnapshot();
	const fixture = _snapshotFixture();
	fixture.mixer.channels[1].stem_eq_mode = true;
	const raw = snapshot.serializePerformanceSession(fixture);
	const parsed = snapshot.parsePerformanceSession(raw);
	assert.notEqual(parsed, null);
	assert.equal(parsed.decks[1].position_ms, 12_345);
	assert.equal(parsed.decks[1].stable_id, 'sid-a');
	assert.equal(parsed.mixer.crossfader, 0.5);
	assert.equal(parsed.mixer.master, 0.8);
	assert.equal(parsed.mixer.channels[1].eq_high, 0.2);
	assert.equal(parsed.mixer.channels[1].fader, 0.7);
	assert.equal(parsed.mixer.channels[1].stem_eq_mode, true);
	assert.equal(parsed.stems[1].vocal.muted, true);
});

test('malformed performance session snapshot returns null', async () => {
	const snapshot = await _loadSnapshot();
	assert.equal(snapshot.parsePerformanceSession('{not json'), null);
	assert.equal(snapshot.parsePerformanceSession('{"version":2}'), null);
});

test('url_playlist_id beats All Tracks when known', async () => {
	const contract = await _loadContract();
	const choice = contract.resolveBootPlaylist({
		remembered: null,
		known_playlist_ids: ['pl-1'],
		all_tracks_count: 10,
		url_playlist_id: 'pl-1'
	});
	assert.deepEqual(choice, { playlist_id: 'pl-1', name: 'pl-1', kind: 'playlist' });
});

test('unknown url_playlist_id falls through to All Tracks', async () => {
	const contract = await _loadContract();
	const bootPane = await loadTypeScriptModule('src/lib/components/rb/browser/boot-pane-selection.ts');
	const choice = contract.resolveBootPlaylist({
		remembered: null,
		known_playlist_ids: ['pl-1'],
		all_tracks_count: 10,
		url_playlist_id: 'pl-missing'
	});
	assert.deepEqual(choice, bootPane.ALL_TRACKS_CHOICE);
});

test('installPerformanceSessionRestore does not throw when window has no location', async () => {
	const session = await _loadSession();
	const replaceCalls = [];
	globalThis.window = {
		localStorage: {
			getItem: () => null,
			setItem: () => {},
			removeItem: () => {}
		}
	};
	try {
		const dispose = session.installPerformanceSessionRestore({
			dispatch: async () => {},
			query: () => ({
				browser: { active_playlist: null },
				mixer: { crossfader: 0.5, master: 1, channels: {} },
				decks: {}
			}),
			replaceState: (url) => replaceCalls.push(url)
		});
		assert.equal(typeof dispose, 'function');
		dispose();
		assert.equal(replaceCalls.length, 0);
	} finally {
		delete globalThis.window;
	}
});

test('installPerformanceSessionRestore with explicit undefined location does not throw', async () => {
	const session = await _loadSession();
	const store = new Map();
	const replaceCalls = [];
	const dispose = session.installPerformanceSessionRestore({
		location: undefined,
		storage: {
			getItem: (key) => (store.has(key) ? store.get(key) : null),
			setItem: (key, value) => store.set(key, value)
		},
		dispatch: async () => {},
		query: () => ({
			browser: { active_playlist: null },
			mixer: { crossfader: 0.5, master: 1, channels: {} },
			decks: {}
		}),
		replaceState: (url) => replaceCalls.push(url)
	});
	assert.equal(typeof dispose, 'function');
	dispose();
	assert.equal(replaceCalls.length, 0);
});

test('6a812c8ac2e5: session snapshot writer throttles interval writes and flushes on pagehide', async () => {
	const session = await _loadSession();
	const snapshot = await _loadSnapshot();
	const store = new Map();
	let now = 0;
	const replaceCalls = [];
	const listeners = new Map();

	const query = () => ({
		browser: { active_playlist: 'pl-1' },
		mixer: {
			crossfader: 0.5,
			master: 0.75,
			channels: {
				1: { trim: 0.5, eq_high: 0.5, eq_mid: 0.5, eq_low: 0.5, filter: 0.5, fader: 1, assign: 'THRU' },
				2: { trim: 0.5, eq_high: 0.5, eq_mid: 0.5, eq_low: 0.5, filter: 0.5, fader: 1, assign: 'THRU' },
				3: { trim: 0.5, eq_high: 0.5, eq_mid: 0.5, eq_low: 0.5, filter: 0.5, fader: 1, assign: 'THRU' },
				4: { trim: 0.5, eq_high: 0.5, eq_mid: 0.5, eq_low: 0.5, filter: 0.5, fader: 1, assign: 'THRU' }
			}
		},
		decks: {
			1: {
				stable_id: 'sid-a',
				position_ms: 1000,
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
			},
			2: {
				stable_id: null,
				position_ms: 0,
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
			},
			3: {
				stable_id: null,
				position_ms: 0,
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
			},
			4: {
				stable_id: null,
				position_ms: 0,
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
			}
		}
	});

	const location = {
		pathname: '/performance',
		search: '',
		href: 'http://127.0.0.1/performance'
	};

	const writer = session.createSessionSnapshotWriter({
		now: () => now,
		storage: {
			getItem: (key) => (store.has(key) ? store.get(key) : null),
			setItem: (key, value) => store.set(key, value)
		},
		location,
		replaceState: (url) => replaceCalls.push(url),
		query,
		isLive: () => true,
		operatorMaster: () => null,
		throttle_ms: 10_000,
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
	});

	writer.flush(true);
	assert.equal(store.size, 1);
	assert.equal(replaceCalls.length, 1);
	assert.equal(replaceCalls[0], '/performance?d1=sid-a');
	assert.doesNotMatch(replaceCalls[0], /\?\?/);
	const first = store.get(snapshot.PERFORMANCE_SESSION_STORAGE_KEY);

	now = 1_000;
	writer.flush(false);
	assert.equal(store.get(snapshot.PERFORMANCE_SESSION_STORAGE_KEY), first);

	now = 2_000;
	listeners.get('window:pagehide')();
	const afterHide = store.get(snapshot.PERFORMANCE_SESSION_STORAGE_KEY);
	assert.notEqual(afterHide, first);

	now = 12_000;
	writer.flush(false);
	const afterInterval = store.get(snapshot.PERFORMANCE_SESSION_STORAGE_KEY);
	assert.notEqual(afterInterval, afterHide);

	assert.equal(listeners.has('window:audioprocess'), false);
	assert.deepEqual([...listeners.keys()].sort(), ['document:visibilitychange', 'window:pagehide']);
});
