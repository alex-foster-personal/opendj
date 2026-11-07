// requirement: CUSTSCR-9441
// [if] a Decks row names no Settings catalog entry, or one that is unimplemented or not boolean/enum [then] this fails, [else stop].
// [if] a Decks id is missing from ALLOWED_SETTING_KEYS [then] this fails, [else stop].
// [if] resolveCustomiseSetting does not refuse missing, unimplemented, or non-boolean-enum rows [then] this fails, [else stop].
// [if] CustomiseScreen.svelte does not use applySettingChange and readSettingValue, or uses uiPrefs or waveQsRowTitle [then] this fails, [else stop].
// requirement: none (data-only screen definition, #9443)
// [if] a library row names a missing, unimplemented or non boolean/enum catalog key [then] this fails, [else stop].
// [if] library density stops being the compact|cosy enum or a row px setting appears [then] this fails, [else stop].
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

/** Pinned display order for the Decks screen (spike Decks 12). */
const DECKS_IDS_ORDER = [
	'deck_layout',
	'deck_layout_animate',
	'deck_layout_duration_ms',
	'deck_left_mirror',
	'deck_right_mirror',
	'deck_hover_link',
	'lyrics_deck_line',
	'deck_default_quantize',
	'beat_sync_max',
	'auto_play_enabled',
	'auto_play_enforce_order',
	'auto_play_maximize_reach'
];

let customiseScreens;
let catalog;
let ssr;

before(async () => {
	customiseScreens = await loadTypeScriptModule('src/lib/settings/customise-screens.ts');
	catalog = await loadTypeScriptModule('src/lib/settings/catalog.ts');
	ssr = await loadSvelteSsrModule(
		"export { default as CustomiseScreen } from '$lib/components/rb/customise/CustomiseScreen.svelte';"
	);
});

/** ALLOWED_SETTING_KEYS read out of apply.ts source (the module itself needs the rune singleton). */
function allowedSettingKeys() {
	const src = readFileSync('src/lib/settings/apply.ts', 'utf8');
	const block = src.match(/export const ALLOWED_SETTING_KEYS = \[([\s\S]*?)\] as const;/);
	assert.ok(block, 'could not read ALLOWED_SETTING_KEYS out of apply.ts');
	return new Set([...block[1].matchAll(/'([^']+)'/g)].map((m) => m[1]));
}

function allScreenRowIds(screen) {
	return screen.groups.flatMap((g) => g.rows.map((r) => r.row.id));
}

test('registry: screen ids (waveform prepends when #9439 lands)', () => {
	assert.deepEqual(customiseScreens.CUSTOMISE_SCREENS.map((s) => s.id), ['decks', 'library']);
});

test('CUSTSCR-9441 decks screen label, ids order and catalogue membership', () => {
	const def = customiseScreens.customiseScreen('decks');
	assert.equal(def.label, 'Decks');
	assert.deepEqual(
		customiseScreens.customiseSettingIds(customiseScreens.customiseScreenDef('decks')),
		DECKS_IDS_ORDER
	);
	const cat = catalog.SETTINGS_CATALOG;
	for (const id of DECKS_IDS_ORDER) {
		assert.ok(cat.find((e) => e.id === id), `${id} missing from SETTINGS_CATALOG`);
	}
});

test('CUSTSCR-9441 every decks id resolves and is allowed', () => {
	const allowed = allowedSettingKeys();
	for (const id of DECKS_IDS_ORDER) {
		const resolved = customiseScreens.resolveCustomiseSetting(catalog.SETTINGS_CATALOG, 'decks', id);
		assert.equal(resolved.id, id);
		assert.equal(resolved.implemented, true);
		assert.ok(
			resolved.control.kind === 'boolean' || resolved.control.kind === 'enum',
			`${id} must be boolean or enum`
		);
		assert.ok(typeof resolved.label === 'string' && resolved.label.length > 0);
		assert.ok(typeof resolved.detail === 'string' && resolved.detail.length > 0);
		assert.ok(allowed.has(id), `${id} is not an applySettingChange key`);
	}
});

test('CUSTSCR-9441 uniqueness and no inert rows on decks', () => {
	const def = customiseScreens.customiseScreen('decks');
	const seen = new Set();
	const groupsSeen = new Set();
	for (const g of def.groups) {
		assert.ok(!groupsSeen.has(g.id), `duplicate group ${g.id}`);
		groupsSeen.add(g.id);
		for (const { row } of g.rows) {
			assert.equal(row.kind, 'setting');
			assert.ok(!seen.has(row.id), `duplicate row ${row.id}`);
			seen.add(row.id);
		}
	}
	const screenIds = new Set(customiseScreens.CUSTOMISE_SCREENS.map((s) => s.id));
	assert.equal(screenIds.size, customiseScreens.CUSTOMISE_SCREENS.length);
});

test('CUSTSCR-9441 resolveCustomiseSetting negative controls', () => {
	assert.throws(
		() => customiseScreens.resolveCustomiseSetting(catalog.SETTINGS_CATALOG, 'decks', 'no_such_setting'),
		/no_such_setting/
	);
	const fake = [{ id: 'x', implemented: false, control: { kind: 'boolean' } }];
	assert.throws(
		() => customiseScreens.resolveCustomiseSetting(fake, 'decks', 'x'),
		/not implemented/
	);
	const num = catalog.SETTINGS_CATALOG.find((d) => d.control.kind === 'number');
	assert.ok(num, 'need a number control in catalog for negative control');
	assert.throws(
		() => customiseScreens.resolveCustomiseSetting(catalog.SETTINGS_CATALOG, 'decks', num.id),
		/boolean and enum/
	);
});

test('CUSTSCR-9441 CustomiseScreen SSR renders decks rows and keeps uiPrefs and waveQsRowTitle out of markup', async () => {
	const { render } = await import('svelte/server');
	const def = customiseScreens.customiseScreenDef('decks');
	const html = render(ssr.CustomiseScreen, { props: { def } }).body;
	assert.match(html, /data-customise-screen="decks"/);
	for (const id of DECKS_IDS_ORDER) {
		assert.match(html, new RegExp(`data-customise-row="${id}"`));
	}
	assert.doesNotMatch(html, /uiPrefs/);
	assert.doesNotMatch(html, /waveQsRowTitle/);
});

test('library: every screen row maps to SETTINGS_CATALOG and allowed keys', () => {
	const allowed = allowedSettingKeys();
	const screen = customiseScreens.customiseScreen('library');
	for (const group of screen.groups) {
		for (const { row, def } of group.rows) {
			assert.equal(row.kind, 'setting');
			assert.ok(def !== null);
			assert.equal(def.id, row.id);
			assert.equal(def.implemented, true);
			assert.ok(
				def.control.kind === 'boolean' || def.control.kind === 'enum',
				`${row.id} must be boolean or enum`
			);
			assert.ok(
				catalog.SETTINGS_CATALOG.some((e) => e.id === row.id),
				`missing catalog entry ${row.id}`
			);
			assert.ok(allowed.has(row.id), `${row.id} is not an applySettingChange key`);
		}
	}
});

test('library group catalog coverage (17 entries; two non-boolean/enum omitted)', () => {
	const screen = customiseScreens.customiseScreen('library');
	const rowIds = new Set(allScreenRowIds(screen));

	const lib = catalog.SETTINGS_CATALOG.filter((e) => e.group === 'library');
	assert.equal(lib.length, 17);

	for (const e of lib) {
		if (e.control.kind === 'boolean' || e.control.kind === 'enum') {
			assert.ok(rowIds.has(e.id), `expected library screen row for ${e.id}`);
		}
	}

	const notOnScreen = lib
		.filter((e) => !rowIds.has(e.id))
		.map((e) => e.id)
		.sort();
	assert.deepEqual(notOnScreen, ['library.title_fix', 'library_watcher_folders']);
	for (const id of notOnScreen) {
		const entry = lib.find((e) => e.id === id);
		assert.ok(entry);
		assert.notEqual(entry.control.kind, 'boolean');
		assert.notEqual(entry.control.kind, 'enum');
	}

	const previewGroup = screen.groups.find((g) => g.id === 'preview');
	assert.ok(previewGroup);
	assert.ok(previewGroup.rows.some((r) => r.row.id === 'preview_beat_sync'));
});

test('library: group layout and unique row ids', () => {
	const screen = customiseScreens.customiseScreen('library');
	assert.deepEqual(
		screen.groups.map((g) => g.id),
		['table', 'columns', 'preview', 'filters', 'sidebar']
	);
	const table = screen.groups.find((g) => g.id === 'table');
	assert.ok(table);
	assert.equal(table.rows[0].row.id, 'library_density');
	const columns = screen.groups.find((g) => g.id === 'columns');
	assert.ok(columns);
	assert.ok(columns.rows.some((r) => r.row.id === 'lyrics_library_col'));

	const ids = allScreenRowIds(screen);
	assert.equal(new Set(ids).size, ids.length);
	assert.equal(ids.length, 16);
});

test('library_density is compact|cosy enum only; no row-px settings in catalog', () => {
	const density = catalog.SETTINGS_CATALOG.find((e) => e.id === 'library_density');
	assert.ok(density);
	assert.equal(density.control.kind, 'enum');
	assert.deepEqual(
		density.control.options.map((o) => o.value),
		['compact', 'cosy']
	);
	for (const o of density.control.options) {
		assert.ok(
			!Number.isFinite(Number.parseFloat(String(o.value))),
			'enum option must not be a number'
		);
	}

	const pxLike = /row_?(height|px)|density_px/i;
	for (const e of catalog.SETTINGS_CATALOG) {
		assert.doesNotMatch(e.id, pxLike);
	}

	const screen = customiseScreens.customiseScreen('library');
	for (const g of screen.groups) {
		for (const { def } of g.rows) {
			assert.ok(def !== null);
			assert.ok(def.control.kind === 'boolean' || def.control.kind === 'enum');
		}
	}
});

test('unknown catalog key fails at import', async () => {
	await assert.rejects(
		loadTypeScriptModule('tests/unit/fixtures/customise-screens-unknown-key.ts'),
		/customise screen bogus: no Settings catalog entry no_such_setting/
	);
});

test('defineCustomiseScreens / customiseScreen negative controls', async () => {
	const { defineCustomiseScreens, customiseScreen, LIBRARY_SCREEN } = customiseScreens;
	const { resolveCustomiseScreen } = await loadTypeScriptModule(
		'src/lib/settings/customise-screen-def.ts'
	);

	assert.throws(
		() =>
			defineCustomiseScreens(catalog.SETTINGS_CATALOG, [
				LIBRARY_SCREEN,
				{ ...LIBRARY_SCREEN, id: 'library' }
			]),
		/customise screens: duplicate screen id library/
	);

	assert.throws(
		() =>
			resolveCustomiseScreen(catalog.SETTINGS_CATALOG, {
				id: 'dup-rows',
				label: 'D',
				groups: [
					{
						id: 'g',
						label: 'G',
						rows: [
							{ kind: 'setting', id: 'library_density' },
							{ kind: 'setting', id: 'library_density' }
						]
					}
				]
			}),
		/customise screen dup-rows: duplicate row id library_density/
	);

	assert.throws(
		() =>
			resolveCustomiseScreen(catalog.SETTINGS_CATALOG, {
				id: 'empty',
				label: 'E',
				groups: [{ id: 'g', label: 'G', rows: [] }]
			}),
		/customise screen empty: group g has no rows/
	);

	assert.throws(
		() =>
			resolveCustomiseScreen(catalog.SETTINGS_CATALOG, { id: 'nogroups', label: 'N', groups: [] }),
		/customise screen nogroups: screen has no groups/
	);

	const unimpl = [{ id: 'x', implemented: false, control: { kind: 'boolean' } }];
	assert.throws(
		() =>
			resolveCustomiseScreen(unimpl, {
				id: 'u',
				label: 'U',
				groups: [{ id: 'g', label: 'G', rows: [{ kind: 'setting', id: 'x' }] }]
			}),
		/not implemented/
	);

	assert.throws(
		() =>
			resolveCustomiseScreen(catalog.SETTINGS_CATALOG, {
				id: 'paths',
				label: 'P',
				groups: [
					{
						id: 'g',
						label: 'G',
						rows: [{ kind: 'setting', id: 'library_watcher_folders' }]
					}
				]
			}),
		/only boolean and enum/
	);

	assert.throws(() => customiseScreen('nope'), /unknown screen nope/);
});
