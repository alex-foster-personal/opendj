import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Remixes + Vocals library filter checkboxes (Tue 1 Sep 2026 review asks).
// Regression lines:
// - if rowIsRemix passes is_remix null (synthetic rows) then the Remixes
//   filter shows tracks that never earned the claim -- broken
// - if rowHasVocalLyrics passes n_lines exactly 5 then '>5 lines' lied -- broken
// - if rowHasVocalLyrics passes rows with no lyric data then unprocessed
//   tracks masquerade as vocal tracks -- broken
// - if the prefs default either filter ON then fresh sessions hide most of
//   the library with no visible cause -- broken
// - if 'remixes_filter'/'vocals_filter' leave ALLOWED_SETTING_KEYS then
//   agent parity for the checkboxes is gone -- broken

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

async function _loadContract() {
	return loadTypeScriptModule('src/lib/components/rb/browser/pane-contract.svelte.ts');
}

function _row(overrides) {
	return { is_remix: null, lyrics: null, ...overrides };
}

test('rowIsRemix passes only an explicit wire true', async () => {
	const c = await _loadContract();
	assert.equal(c.rowIsRemix(_row({ is_remix: true })), true);
	assert.equal(c.rowIsRemix(_row({ is_remix: false })), false);
	assert.equal(c.rowIsRemix(_row({ is_remix: null })), false, 'synthetic rows must not pass');
});

test('rowHasVocalLyrics demands MORE than 5 real lines', async () => {
	const c = await _loadContract();
	assert.equal(c.VOCALS_FILTER_MIN_LINES, 6);
	const withLines = (n) => _row({ lyrics: { n_lines: n } });
	assert.equal(c.rowHasVocalLyrics(withLines(6)), true);
	assert.equal(c.rowHasVocalLyrics(withLines(5)), false, '5 lines is not >5');
	assert.equal(c.rowHasVocalLyrics(withLines(null)), false, 'no alignment yet');
	assert.equal(c.rowHasVocalLyrics(_row({ lyrics: null })), false, 'no lyric data yet');
});

test('CAT-07 (pin c7da76c16aae): rowIsLocallyAvailable keeps only local-audio rows', async () => {
	const c = await _loadContract();
	assert.equal(
		c.rowIsLocallyAvailable(_row({ file_exists: true, is_streaming: false })),
		true
	);
	assert.equal(
		c.rowIsLocallyAvailable(_row({ file_exists: false, has_remote_copy: true })),
		false
	);
	assert.equal(
		c.rowIsLocallyAvailable(_row({ file_exists: true, is_streaming: true })),
		false
	);
	assert.equal(c.rowIsLocallyAvailable(_row({ file_exists: null })), false);
});

test('both filter prefs default OFF and survive a round trip', async () => {
	_fakeWindow();
	const fresh = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts');
	assert.equal(fresh.uiPrefs.remixes_filter, false);
	assert.equal(fresh.uiPrefs.vocals_filter, false);
	assert.equal(fresh.uiPrefs.available_offline_filter, false);
	delete globalThis.window;

	_fakeWindow(
		JSON.stringify({ hide_broken_links: false, remixes_filter: true, vocals_filter: true })
	);
	const stored = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts');
	assert.equal(stored.uiPrefs.remixes_filter, true);
	assert.equal(stored.uiPrefs.vocals_filter, true);
});

test('malformed filter prefs throw rather than silently resetting', async () => {
	for (const bad of [{ remixes_filter: 'yes' }, { vocals_filter: 1 }]) {
		_fakeWindow(JSON.stringify({ hide_broken_links: false, ...bad }));
		await assert.rejects(
			() => loadTypeScriptModule('src/lib/rb/prefs.svelte.ts'),
			/malformed prefs blob/,
			`should reject ${JSON.stringify(bad)}`
		);
		delete globalThis.window;
	}
});

test('both filters have settings agent parity: allowlist, read, apply, catalog', async () => {
	_fakeWindow();
	const apply = await loadTypeScriptModule('src/lib/settings/apply.ts');
	const catalog = await loadTypeScriptModule('src/lib/settings/catalog.ts');

	for (const key of ['remixes_filter', 'vocals_filter', 'available_offline_filter']) {
		assert.ok(apply.ALLOWED_SETTING_KEYS.includes(key), `${key} missing from allowlist`);
		assert.equal(apply.readSettingValue(key), false);
		apply.applySettingChange(key, true);
		assert.equal(apply.readSettingValue(key), true);
		const entry = catalog.SETTINGS_CATALOG.find((e) => e.id === key);
		assert.ok(entry !== undefined, `${key} missing catalog entry`);
		assert.equal(entry.implemented, true);
		assert.equal(entry.group, 'library');
	}
});

test('CAT-07: the browser applies rowIsLocallyAvailable only while the available-offline filter is on', async () => {
	const panel = await readFile('src/lib/components/rb/BrowserPanel.svelte', 'utf8');
	const body = panel.slice(panel.indexOf('function _applyLibraryFilters('), panel.indexOf('function _applyNextOnly('));
	assert.match(body, /if \(uiPrefs\.available_offline_filter\) out = out\.filter\(rowIsLocallyAvailable\);/);
	assert.match(panel, /checked=\{uiPrefs\.available_offline_filter\}/);
});
