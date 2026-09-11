import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Round ui-2 regressions (Tue 1 Sep 2026 review): the global LYR master
// switch and the 11px lane bump. Regression lines:
// - if lyrics_global is absent from DEFAULTS or defaults false then every
//   lyric surface ships dark for everyone -- broken
// - if a pre-existing prefs blob (saved before the key existed) throws or
//   leaves lyrics_global undefined then WaveRow's gate goes dead -- broken
// - if a stored false does not survive the round trip then the master
//   switch un-mutes itself on reload -- broken
// - if setLyricsGlobal does not persist then the toggle is decorative -- broken
// - if 'lyrics_global' leaves ALLOWED_SETTING_KEYS or the catalog then
//   agent parity for the UI toggle is gone -- broken
// - if LANE_FONT_PX outgrows the --rb-waverow-h row (the readability bump
//   was 9 -> 11px) then lyricLaneYs throws at paint time -- broken

/** Install a localStorage-backed fake window so prefs._load reads `raw`. */
function _fakeWindow(raw) {
	const store = new Map();
	if (raw !== undefined) store.set('mdt.rb.ui-prefs.v1', raw);
	globalThis.window = {
		localStorage: {
			getItem: (k) => (store.has(k) ? store.get(k) : null),
			setItem: (k, v) => store.set(k, v)
		}
	};
	return store;
}

afterEach(() => {
	delete globalThis.window;
});

async function _loadPrefs() {
	return loadTypeScriptModule('src/lib/rb/prefs.svelte.ts');
}

test('lyrics_global defaults ON, including for pre-existing blobs', async () => {
	_fakeWindow();
	assert.equal((await _loadPrefs()).uiPrefs.lyrics_global, true);
	delete globalThis.window;

	_fakeWindow(JSON.stringify({ hide_broken_links: false, theme: 'dark' }));
	assert.equal((await _loadPrefs()).uiPrefs.lyrics_global, true);
});

test('a stored lyrics_global false survives the round trip', async () => {
	_fakeWindow(JSON.stringify({ hide_broken_links: false, lyrics_global: false }));
	assert.equal((await _loadPrefs()).uiPrefs.lyrics_global, false);
});

test('a malformed lyrics_global throws rather than silently resetting', async () => {
	for (const bad of ['yes', 1, null]) {
		_fakeWindow(JSON.stringify({ hide_broken_links: false, lyrics_global: bad }));
		await assert.rejects(
			() => _loadPrefs(),
			/malformed prefs blob/,
			`should reject ${JSON.stringify(bad)}`
		);
		delete globalThis.window;
	}
});

test('setLyricsGlobal and toggleLyricsGlobal flip the pref AND persist it', async () => {
	const store = _fakeWindow();
	const prefs = await _loadPrefs();

	prefs.setLyricsGlobal(false);
	assert.equal(prefs.uiPrefs.lyrics_global, false);
	assert.equal(JSON.parse(store.get('mdt.rb.ui-prefs.v1')).lyrics_global, false);

	prefs.toggleLyricsGlobal();
	assert.equal(prefs.uiPrefs.lyrics_global, true);
	assert.equal(JSON.parse(store.get('mdt.rb.ui-prefs.v1')).lyrics_global, true);
});

test('lyrics_global has settings agent parity: allowlist, read, apply, catalog', async () => {
	_fakeWindow();
	const apply = await loadTypeScriptModule('src/lib/settings/apply.ts');
	const catalog = await loadTypeScriptModule('src/lib/settings/catalog.ts');

	assert.ok(apply.ALLOWED_SETTING_KEYS.includes('lyrics_global'), 'missing from allowlist');
	assert.equal(apply.readSettingValue('lyrics_global'), true);
	apply.applySettingChange('lyrics_global', false);
	assert.equal(apply.readSettingValue('lyrics_global'), false);

	const entry = catalog.SETTINGS_CATALOG.find((e) => e.id === 'lyrics_global');
	assert.ok(entry !== undefined, 'missing catalog entry');
	assert.equal(entry.implemented, true);
});

test('the 11px lane font still fits the theme wave-row height', async () => {
	const lanes = await loadTypeScriptModule('src/lib/components/rb/wave/word-lanes.ts');

	const css = readFileSync(join(process.cwd(), 'src/lib/rb/theme.css'), 'utf8');
	const m = css.match(/--rb-waverow-h:\s*(\d+)px/);
	assert.ok(m !== null, 'theme.css lost --rb-waverow-h');
	const rowH = Number(m[1]);

	assert.equal(lanes.LANE_FONT_PX, 11, 'readability bump regressed');
	const [lane0, lane1] = lanes.lyricLaneYs(rowH);
	assert.ok(lane0 < lane1 && lane1 <= rowH, `lanes ${lane0},${lane1} overflow ${rowH}px row`);
	assert.throws(() => lanes.lyricLaneYs(20), RangeError, 'undersized row must fail fast');
});
