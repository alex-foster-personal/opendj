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
	mute = await loadTypeScriptModule('src/lib/player/master-mute.svelte.ts', {
		viteApiBase: API_BASE
	});
	prefsHydrate = await loadTypeScriptModule('src/lib/rb/prefs-hydrate.ts', {
		viteApiBase: API_BASE
	});
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

test('makePrefsHydrator applies disk master_muted through setMasterMuted', async () => {
	globalThis.fetch = async () => jsonResponse({ master_muted: true });

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

	const hydrate = prefsHydrate.makePrefsHydrator({
		uiPrefs,
		persist: () => {},
		applyThemeDom: () => {},
		storageKey: 'mdt.rb.ui-prefs.v1',
		defaults: {
			auto_sync: uiPrefs.auto_sync,
			level_calibration: uiPrefs.level_calibration
		}
	});

	await hydrate();
	assert.equal(mute.isMasterMuted(), true);
});
