import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://master-mute-prefs.example.test';

let mute;
let prefsHydrate;
let originalFetch;

function jsonResponse(body, init = {}) {
	return new Response(JSON.stringify(body), {
		status: 200,
		...init,
		headers: { 'content-type': 'application/json', ...(init.headers ?? {}) }
	});
}

before(async () => {
	({ mute, prefsHydrate } = await loadTypeScriptModule('tests/unit/fixtures/master-mute-hydrate-entry.ts', {
		viteApiBase: API_BASE
	}));
	originalFetch = globalThis.fetch;
	globalThis.window = {
		localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} }
	};
});

after(() => {
	globalThis.fetch = originalFetch;
	delete globalThis.window;
});

test('setMasterMuted PUTs master_muted to /api/v1/ui-prefs', async () => {
	let seen;
	let body;
	let release;
	const gate = new Promise((resolve) => {
		release = resolve;
	});
	globalThis.fetch = async (request) => {
		seen = request;
		body = await request.clone().json();
		release();
		return jsonResponse({ master_muted: true });
	};

	mute.setMasterMuted(true);
	await gate;

	assert.equal(seen.url, `${API_BASE}/api/v1/ui-prefs`);
	assert.equal(seen.method, 'PUT');
	assert.deepEqual(body, { master_muted: true });
});

function hydratorArgs() {
	const uiPrefs = {
		confirm: {},
		theme: 'dark',
		hide_todo_settings: false,
		auto_sync: { rekordbox: false, djay: false, open_dj: false },
		technically_working_animate: true,
		show_agent_pins: true,
		jog_radial_waveform: false,
		deck_layout: 'more',
		deck_layout_animate: true,
		deck_layout_duration_ms: 300,
		level_calibration: {
			red_dbfs: null,
			red_enabled: false,
			ceiling_dbfs: null,
			ceiling_enabled: false
		},
		last_playlist: null,
		lyrics_global: true,
		lyrics_library_col: true,
		lyrics_hover_scrub: true,
		lyrics_load_strategy: 'hover',
		lyrics_waveform_overlay: true,
		lyrics_deck_line: true,
		perf_tier: 'auto',
		app_posture: 'prep',
		app_mode: { id: 'performance', last_gig_at: null },
		beat_sync_max: true,
		auto_play_enabled: true,
		auto_play_enforce_order: false,
		auto_play_maximize_reach: true,
		hide_broken_links: false,
		library_density: 'compact',
		next_only_filter: false,
		remixes_filter: false,
		vocals_filter: false
	};

	return {
		uiPrefs,
		persist: () => {},
		applyThemeDom: () => {},
		storageKey: 'mdt.rb.ui-prefs.v1',
		defaults: {
			auto_sync: uiPrefs.auto_sync,
			level_calibration: uiPrefs.level_calibration
		}
	};
}

test('makePrefsHydrator applies disk master_muted through setMasterMuted', async () => {
	globalThis.fetch = async () => jsonResponse({ master_muted: true });

	const hydrate = prefsHydrate.makePrefsHydrator(hydratorArgs());

	await hydrate();
	assert.equal(mute.isMasterMuted(), true);
	assert.equal(mute.masterMuteReason(), null, 'already muted in this profile: no disk reason');
});

function memoryStorage(initial) {
	const store = new Map(Object.entries(initial));
	return {
		store,
		getItem: (key) => (store.has(key) ? store.get(key) : null),
		setItem: (key, value) => store.set(key, String(value)),
		removeItem: (key) => store.delete(key)
	};
}

test('hydrate with master_muted=false clears a stale stored mute', async () => {
	const storage = memoryStorage({ [mute.MASTER_MUTE_STORAGE_KEY]: '1' });
	globalThis.window.localStorage = storage;
	const puts = [];
	globalThis.fetch = async (request) => {
		if (request.method === 'PUT') puts.push(await request.clone().json());
		return jsonResponse({ master_muted: false });
	};
	mute.setMasterMuted(true, { persist: false });
	assert.equal(mute.isMasterMuted(), true);

	await prefsHydrate.makePrefsHydrator(hydratorArgs())();

	assert.equal(mute.isMasterMuted(), false, 'if disk false does not unmute then a stale stored mute silences the maintainer');
	assert.equal(storage.store.has(mute.MASTER_MUTE_STORAGE_KEY), false, 'stale odj.master-muted.v1 must be removed');
	assert.deepEqual(puts, [], 'hydrate must not write back the value it just read');
});

test('hydrate with master_muted=true on an audible page mutes with a visible reason', async () => {
	globalThis.window.localStorage = memoryStorage({});
	globalThis.fetch = async () => jsonResponse({ master_muted: true });
	mute.setMasterMuted(false, { persist: false });

	await prefsHydrate.makePrefsHydrator(hydratorArgs())();

	assert.equal(mute.isMasterMuted(), true);
	assert.equal(mute.masterMuteReason(), mute.MASTER_MUTE_DISK_REASON, 'if a disk mute has no reason then the headed client is silenced without a word');
});

test('setMasterMuted with persist:false writes neither localStorage nor disk', async () => {
	const storage = memoryStorage({});
	globalThis.window.localStorage = storage;
	let fetched = false;
	globalThis.fetch = async () => {
		fetched = true;
		return jsonResponse({});
	};

	mute.setMasterMuted(false);
	await new Promise((resolve) => setTimeout(resolve, 20));
	fetched = false;
	mute.setMasterMuted(true, { persist: false });
	await new Promise((resolve) => setTimeout(resolve, 20));

	assert.equal(mute.isMasterMuted(), true);
	assert.equal(storage.store.size, 0, 'if a safety mute is stored then this profile starts muted');
	assert.equal(fetched, false, 'if a safety mute reaches ui-prefs then every browser starts muted');
});
