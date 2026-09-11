import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// The six karaoke lyric prefs (PR-4 section C). Regression lines:
// - if a lyric key is absent from DEFAULTS or defaults off then every lyric
//   surface ships dark for everyone -- broken
// - if a pre-existing prefs blob (saved before the keys existed) throws or
//   leaves a lyric key undefined then the surface gates go dead -- broken
// - if a stored false / 'off' does not survive the round trip then the
//   switches un-mute themselves on reload -- broken
// - if a wrong-typed stored key silently resets rather than throwing then a
//   corrupt blob is indistinguishable from first run -- broken
// - if a setter does not persist AND PUT /api/v1/ui-prefs then the toggle is
//   decorative, or the agent-parity HTTP twin drifts from the UI -- broken
// - if a key leaves ALLOWED_SETTING_KEYS or the catalog then agent parity for
//   that toggle is gone -- broken

const STORAGE_KEY = 'mdt.rb.ui-prefs.v1';
const API_BASE = 'https://lyrics-prefs.example.test';
const LYRIC_DEFAULTS = {
	lyrics_global: true,
	lyrics_library_col: true,
	lyrics_hover_scrub: true,
	lyrics_load_strategy: 'hover',
	lyrics_waveform_overlay: true,
	lyrics_deck_line: true
};
/** setter name -> [pref key, value it writes]. */
const SETTERS = {
	setLyricsGlobal: ['lyrics_global', false],
	setLyricsLibraryCol: ['lyrics_library_col', false],
	setLyricsHoverScrub: ['lyrics_hover_scrub', false],
	setLyricsLoadStrategy: ['lyrics_load_strategy', 'off'],
	setLyricsWaveformOverlay: ['lyrics_waveform_overlay', false],
	setLyricsDeckLine: ['lyrics_deck_line', false]
};

/** Install a localStorage-backed fake window so prefs._load reads `raw`. */
function _fakeWindow(raw) {
	const store = new Map();
	if (raw !== undefined) store.set(STORAGE_KEY, raw);
	globalThis.window = {
		localStorage: {
			getItem: (k) => (store.has(k) ? store.get(k) : null),
			setItem: (k, v) => store.set(k, v)
		}
	};
	return store;
}

function _stored(store) {
	return JSON.parse(store.get(STORAGE_KEY));
}

async function _loadPrefs() {
	return loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE });
}

/** Capture the bodies the disk-sync chain PUTs, without a real daemon. */
function _capturePuts() {
	const bodies = [];
	const original = globalThis.fetch;
	globalThis.fetch = async (request) => {
		bodies.push(await request.clone().json());
		return new Response(JSON.stringify({ theme: 'dark' }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};
	return {
		bodies,
		restore: () => {
			globalThis.fetch = original;
		}
	};
}

afterEach(() => {
	delete globalThis.window;
});

test('all six lyric prefs default on, with hover loading', async () => {
	_fakeWindow();
	const { uiPrefs } = await _loadPrefs();
	for (const [key, want] of Object.entries(LYRIC_DEFAULTS)) {
		assert.equal(uiPrefs[key], want, `${key} default`);
	}
});

test('a prefs blob written before the lyric keys existed loads with defaults', async () => {
	_fakeWindow(JSON.stringify({ hide_broken_links: false, theme: 'dark' }));
	const { uiPrefs } = await _loadPrefs();
	for (const [key, want] of Object.entries(LYRIC_DEFAULTS)) {
		assert.equal(uiPrefs[key], want, `${key} must default for a pre-existing blob`);
	}
});

test('stored false / off values survive the round trip', async () => {
	const off = {
		hide_broken_links: false,
		lyrics_global: false,
		lyrics_library_col: false,
		lyrics_hover_scrub: false,
		lyrics_load_strategy: 'off',
		lyrics_waveform_overlay: false,
		lyrics_deck_line: false
	};
	_fakeWindow(JSON.stringify(off));
	const { uiPrefs } = await _loadPrefs();
	assert.equal(uiPrefs.lyrics_global, false);
	assert.equal(uiPrefs.lyrics_library_col, false);
	assert.equal(uiPrefs.lyrics_hover_scrub, false);
	assert.equal(uiPrefs.lyrics_load_strategy, 'off');
	assert.equal(uiPrefs.lyrics_waveform_overlay, false);
	assert.equal(uiPrefs.lyrics_deck_line, false);
});

test("a stored 'in-view' strategy survives the round trip", async () => {
	_fakeWindow(JSON.stringify({ hide_broken_links: false, lyrics_load_strategy: 'in-view' }));
	assert.equal((await _loadPrefs()).uiPrefs.lyrics_load_strategy, 'in-view');
});

test('a wrong-typed lyric boolean throws, naming the storage key', async () => {
	for (const key of Object.keys(LYRIC_DEFAULTS)) {
		if (key === 'lyrics_load_strategy') continue;
		for (const bad of ['yes', 1, null]) {
			_fakeWindow(JSON.stringify({ hide_broken_links: false, [key]: bad }));
			await assert.rejects(
				() => _loadPrefs(),
				new RegExp(`${STORAGE_KEY}: malformed prefs blob \\(${key} is not a boolean\\)`),
				`${key} should reject ${JSON.stringify(bad)}`
			);
			delete globalThis.window;
		}
	}
});

test('an unknown lyrics_load_strategy throws rather than silently resetting', async () => {
	for (const bad of ['eager', true, 3]) {
		_fakeWindow(JSON.stringify({ hide_broken_links: false, lyrics_load_strategy: bad }));
		await assert.rejects(
			() => _loadPrefs(),
			new RegExp(
				`${STORAGE_KEY}: malformed prefs blob \\(lyrics_load_strategy must be in-view\\|hover\\|off\\)`
			),
			`should reject ${JSON.stringify(bad)}`
		);
		delete globalThis.window;
	}
});

test('every lyric setter persists to localStorage and PUTs its own patch', async () => {
	for (const [setter, [key, value]] of Object.entries(SETTERS)) {
		const store = _fakeWindow();
		const puts = _capturePuts();
		try {
			const prefs = await _loadPrefs();
			prefs[setter](value);
			assert.equal(prefs.uiPrefs[key], value, `${setter} did not set ${key}`);
			assert.equal(_stored(store)[key], value, `${setter} did not persist ${key}`);
			// The disk-sync chain is async; one microtask drain is enough for
			// the queued PUT to reach the stubbed fetch.
			await new Promise((resolve) => setTimeout(resolve, 0));
			assert.deepEqual(puts.bodies, [{ [key]: value }], `${setter} disk patch`);
		} finally {
			puts.restore();
			delete globalThis.window;
		}
	}
});

test('toggleLyricsGlobal flips the master switch and persists both ways', async () => {
	const store = _fakeWindow();
	const puts = _capturePuts();
	try {
		const prefs = await _loadPrefs();

		prefs.toggleLyricsGlobal();
		assert.equal(prefs.uiPrefs.lyrics_global, false);
		assert.equal(_stored(store).lyrics_global, false);

		prefs.toggleLyricsGlobal();
		assert.equal(prefs.uiPrefs.lyrics_global, true);
		assert.equal(_stored(store).lyrics_global, true);

		await new Promise((resolve) => setTimeout(resolve, 0));
		assert.deepEqual(puts.bodies, [{ lyrics_global: false }, { lyrics_global: true }]);
	} finally {
		puts.restore();
	}
});

test('hydrateConfirmPrefsFromDisk applies all six lyric prefs from disk', async () => {
	_fakeWindow();
	const prefs = await _loadPrefs();
	const original = globalThis.fetch;
	globalThis.fetch = async () =>
		new Response(
			JSON.stringify({
				theme: 'dark',
				lyrics_global: false,
				lyrics_library_col: false,
				lyrics_hover_scrub: false,
				lyrics_load_strategy: 'in-view',
				lyrics_waveform_overlay: false,
				lyrics_deck_line: false
			}),
			{ status: 200, headers: { 'content-type': 'application/json' } }
		);
	try {
		await prefs.hydrateConfirmPrefsFromDisk();
		assert.equal(prefs.uiPrefs.lyrics_global, false);
		assert.equal(prefs.uiPrefs.lyrics_library_col, false);
		assert.equal(prefs.uiPrefs.lyrics_hover_scrub, false);
		assert.equal(prefs.uiPrefs.lyrics_load_strategy, 'in-view');
		assert.equal(prefs.uiPrefs.lyrics_waveform_overlay, false);
		assert.equal(prefs.uiPrefs.lyrics_deck_line, false);
	} finally {
		globalThis.fetch = original;
	}
});

test('hydrate ignores an unknown lyrics_load_strategy from disk', async () => {
	_fakeWindow();
	const prefs = await _loadPrefs();
	const original = globalThis.fetch;
	globalThis.fetch = async () =>
		new Response(JSON.stringify({ theme: 'dark', lyrics_load_strategy: 'eager' }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	try {
		await prefs.hydrateConfirmPrefsFromDisk();
		assert.equal(prefs.uiPrefs.lyrics_load_strategy, 'hover');
	} finally {
		globalThis.fetch = original;
	}
});

test('the six lyric prefs have settings agent parity: allowlist, read, apply, catalog', async () => {
	_fakeWindow();
	const apply = await loadTypeScriptModule('src/lib/settings/apply.ts', { viteApiBase: API_BASE });
	const catalog = await loadTypeScriptModule('src/lib/settings/catalog.ts');
	const original = globalThis.fetch;
	globalThis.fetch = async () =>
		new Response(JSON.stringify({ theme: 'dark' }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	try {
		for (const [key, def] of Object.entries(LYRIC_DEFAULTS)) {
			assert.ok(apply.ALLOWED_SETTING_KEYS.includes(key), `${key} missing from allowlist`);
			assert.equal(apply.readSettingValue(key), def, `${key} read`);
			const entry = catalog.SETTINGS_CATALOG.find((e) => e.id === key);
			assert.ok(entry !== undefined, `${key} missing catalog entry`);
			assert.equal(entry.implemented, true, `${key} must not be a PARITY-TODO stub`);
		}
		assert.equal(catalog.SETTINGS_CATALOG.filter((e) => e.id.startsWith('lyrics_')).length, 6);

		apply.applySettingChange('lyrics_global', false);
		assert.equal(apply.readSettingValue('lyrics_global'), false);
		apply.applySettingChange('lyrics_load_strategy', 'in-view');
		assert.equal(apply.readSettingValue('lyrics_load_strategy'), 'in-view');
	} finally {
		globalThis.fetch = original;
	}
});

test('applySettingChange refuses an unknown lyrics_load_strategy', async () => {
	_fakeWindow();
	const apply = await loadTypeScriptModule('src/lib/settings/apply.ts', { viteApiBase: API_BASE });
	assert.throws(
		() => apply.applySettingChange('lyrics_load_strategy', 'eager'),
		/lyrics_load_strategy must be in-view\|hover\|off, got eager/
	);
});
