import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Remixes + Vocals library filter checkboxes (Tue 1 Sep 2026 review asks).
// Regression lines:
// - if rowIsRemix passes is_remix null (synthetic rows) then the Remixes
//   filter shows tracks that never earned the claim -- broken
// - if the Vocals filter passes 5 lines then a one-line hook counts as vocals -- broken
// - if the Vocals filter passes rows with no vocal or lyric evidence then
//   unanalyzed tracks masquerade as vocal tracks -- broken
// - if the Vocals filter reads only lyrics.n_lines then tracks with vocal
//   regions or a 'vocal' verdict vanish (VOCALS-FILTER-01) -- broken
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

// VOCALS-FILTER-01 (the maintainer, Tue 6 Oct 2026: "filtering by vocals shows very few
// songs on all tracks - check for bug"): the checkbox means "has vocals".
const _REGION = { start_s: 1, end_s: 9, intensity: 3 };
const _lyrics = (effective, n_lines) => ({ effective, n_lines });
const VOCALS_CASES = [
	// [label, vocals, lyrics, verdict]
	['rekordbox regions', { status: 'rekordbox', fps: 150, regions: [_REGION] }, null, 'yes'],
	['demucs regions', { status: 'demucs', fps: 10, regions: [_REGION] }, null, 'yes'],
	['rekordbox, zero regions', { status: 'rekordbox', fps: 150, regions: [] }, null, 'no'],
	['demucs, none found', { status: 'demucs', fps: 10, regions: [] }, null, 'no'],
	['no_vocals', { status: 'no_vocals', fps: 150, regions: [] }, null, 'no'],
	['not analyzed, no lyric data', { status: 'not_analyzed' }, null, 'unknown'],
	['not analyzed, 12 cached lines', { status: 'not_analyzed' }, _lyrics('unknown', 12), 'yes'],
	['not analyzed, exactly 6 lines', { status: 'not_analyzed' }, _lyrics('unknown', 6), 'yes'],
	['not analyzed, 5 lines (a hook)', { status: 'not_analyzed' }, _lyrics('unknown', 5), 'unknown'],
	['not analyzed, vocal verdict', { status: 'not_analyzed' }, _lyrics('vocal', null), 'yes'],
	['not analyzed, sparse verdict', { status: 'not_analyzed' }, _lyrics('sparse', null), 'unknown'],
	['not analyzed, no-lyrics verdict', { status: 'not_analyzed' }, _lyrics('no-lyrics', null), 'no'],
	['no_vocals but 20 lines of lyrics', { status: 'no_vocals', fps: 150, regions: [] }, _lyrics('unknown', 20), 'yes'],
	// Sol P1 on 4c6107765: a no-lyrics verdict outranks the track's own line count...
	['no-lyrics verdict over 12 lines', { status: 'not_analyzed' }, _lyrics('no-lyrics', 12), 'no'],
	// ...but not real vocal regions.
	['no-lyrics verdict, rekordbox regions', { status: 'rekordbox', fps: 150, regions: [_REGION] }, _lyrics('no-lyrics', 12), 'yes']
];

test('VOCALS-FILTER-01: each vocals/lyrics status gets the expected verdict', async () => {
	const f = await loadTypeScriptModule('src/lib/components/rb/browser/vocals-filter.ts');
	const c = await _loadContract();
	assert.equal(c.VOCALS_FILTER_MIN_LINES, 6);
	for (const [label, vocals, lyrics, verdict] of VOCALS_CASES) {
		const row = _row({ vocals, lyrics });
		assert.equal(f.rowVocalsVerdict(row), verdict, label);
		const vocalsOnly = { remixes_filter: false, vocals_filter: true, available_offline_filter: false, library_key_filter: [] };
		assert.equal(c.applyLibraryFilters([row], vocalsOnly).length === 1, verdict === 'yes', `filter: ${label}`);
	}
});

test('VOCALS-FILTER-01 mutation: the old n_lines-only rule fails the fixture', () => {
	// The pre-fix predicate, verbatim in behavior: lyrics.n_lines >= 6 or nothing.
	const oldRule = (row) => row.lyrics !== null && row.lyrics.n_lines !== null && row.lyrics.n_lines >= 6;
	const wrong = VOCALS_CASES.filter(([, vocals, lyrics, verdict]) => oldRule(_row({ vocals, lyrics })) !== (verdict === 'yes'));
	assert.deepEqual(
		wrong.map(([label]) => label),
		['rekordbox regions', 'demucs regions', 'not analyzed, vocal verdict', 'no-lyrics verdict over 12 lines'],
		'the fixture must catch a revert to the n_lines-only rule'
	);
});

test('VOCALS-FILTER-01: the tooltip states the rule and the counts', async () => {
	const c = await _loadContract();
	const rows = VOCALS_CASES.map(([, vocals, lyrics]) => _row({ vocals, lyrics }));
	const counts = c.vocalsFilterCounts(rows);
	assert.deepEqual(counts, { matching: 7, without: 5, unknown: 3 });
	const title = c.vocalsFilterTitle(counts);
	assert.match(title, /vocal regions from rekordbox or stem detection/);
	assert.match(title, /at least 6 lines of lyrics/);
	assert.match(title, /7 with vocals, 5 without, 3 not analyzed yet/);
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
	// BrowserPanel feeds its prefs to pane-contract applyLibraryFilters (#7599 moved it there).
	assert.match(panel, /applyLibraryFilters\(rows, uiPrefs\)/);
	assert.match(panel, /checked=\{uiPrefs\.available_offline_filter\}/);
	const c = await _loadContract();
	const local = _row({ stable_id: 'local', file_exists: true, is_streaming: false });
	const cloud = _row({ stable_id: 'cloud', file_exists: false, has_remote_copy: true });
	const off = { remixes_filter: false, vocals_filter: false, available_offline_filter: false, library_key_filter: [] };
	assert.deepEqual(c.applyLibraryFilters([local, cloud], off).map((r) => r.stable_id), ['local', 'cloud']);
	assert.deepEqual(
		c.applyLibraryFilters([local, cloud], { ...off, available_offline_filter: true }).map((r) => r.stable_id),
		['local']
	);
});
