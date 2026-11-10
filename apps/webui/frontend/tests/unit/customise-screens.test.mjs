// requirement: CUSTSCR-10464
// [if] resolveCustomiseSetting or resolveWaveQsSetting reject a number catalog entry [then] this fails, [else stop].
// [if] path_lines, link or multi_bool still resolve [then] this fails, [else stop].
// [if] stepSettingNumber at max with plus, or stepperKeySteps/parse rules drift [then] this fails, [else stop].
// [if] SSR stepper markup is missing spinbutton, unit or bound buttons [then] this fails, [else stop].
// requirement: CUSTSCR-9441
// [if] a Decks row names no Settings catalog entry, or one that is unimplemented or not boolean, enum or number [then] this fails, [else stop].
// [if] a Decks id is missing from ALLOWED_SETTING_KEYS [then] this fails, [else stop].
// [if] resolveCustomiseSetting does not refuse missing, unimplemented, or non-boolean-enum rows [then] this fails, [else stop].
// [if] CustomiseScreen.svelte does not use applySettingChange and readSettingValue, or uses uiPrefs or waveQsRowTitle [then] this fails, [else stop].
// requirement: none (data-only screen definition, #9443)
// [if] a library row names a missing, unimplemented or non boolean, enum or number catalog key [then] this fails, [else stop].
// [if] library density stops being the compact|cosy enum or a row px setting appears [then] this fails, [else stop].
// requirement: CUSTSCR-10206
// [if] an implemented catalog id is in no screen and not in SETTINGS_ONLY [then] this fails, [else stop].
// [if] an id appears in two screens, or in a screen and SETTINGS_ONLY [then] this fails, [else stop].
// [if] a SETTINGS_ONLY row has an empty reason or names an id not in the catalog [then] this fails, [else stop].
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

/** Pinned display order for the Decks screen (spike Decks 12). */
const DECKS_IDS_ORDER = [
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
let waveQs;
let ssr;
let numberStep;

const NUMBER_FAKE = [
	{
		id: 'n',
		label: 'N',
		title: 'N',
		detail: 'N',
		implemented: true,
		control: { kind: 'number', min: 0, max: 100, step: 5, defaultValue: 50, unit: ' %' }
	}
];

const STEP_CONTROL = { kind: 'number', min: 0, max: 100, step: 5, defaultValue: 50, unit: ' %' };

/** Spike #9438 §3 Settings-only rows (19); bucket must always seed these. */
const SPIKE_SETTINGS_ONLY_19 = [
	'confirm.dblclick_load_play',
	'confirm.delete_playlist',
	'confirm.playlist_drop_mode',
	'auto_sync',
	'sync.run_ratings_writeback',
	'cloudsync.machines',
	'cloudsync.playlist_pins',
	'cloudsync.overview',
	'perf_tier',
	'app_posture',
	'gig_helper',
	'audio_engine',
	'rb.midi_enabled',
	'speaker_auto_recover',
	'show_dev_ui',
	'hide_todo_settings',
	'pin_visible_days',
	'lyrics_global',
	'technically_working_animate'
];

/**
 * lyrics_hover_scrub is on Library (spike §2.10) and in WAVE_QS_GROUPS until #9439 decides placement.
 * Only this id may overlap LIBRARY_SCREEN and WAVE_QS_GROUPS; any other overlap fails coverage.
 */
const KNOWN_STANDIN_OVERLAP = new Set(['lyrics_hover_scrub']);

/**
 * @param {readonly { id: string; implemented?: boolean }[]} catalogEntries
 * @param {Array<{ name: string; ids: string[] }>} places
 * @param {readonly { id: string; reason: string }[]} bucket
 * @param {Set<string>} [allowedOverlap]
 * @returns {string[]}
 */
function coverageProblems(catalogEntries, places, bucket, allowedOverlap = new Set()) {
	const problems = [];
	const catalogById = new Map(catalogEntries.map((e) => [e.id, e]));
	const implementedIds = catalogEntries.filter((e) => e.implemented).map((e) => e.id);

	const idToPlaces = new Map();
	for (const place of places) {
		for (const id of place.ids) {
			if (!idToPlaces.has(id)) idToPlaces.set(id, []);
			idToPlaces.get(id).push(place.name);
			if (!catalogById.has(id)) {
				problems.push(`${place.name} ${id}: not in SETTINGS_CATALOG`);
			}
		}
	}

	const bucketIdCounts = new Map();
	const bucketIds = new Set();
	for (const row of bucket) {
		bucketIdCounts.set(row.id, (bucketIdCounts.get(row.id) ?? 0) + 1);
		bucketIds.add(row.id);
		if (!catalogById.has(row.id)) {
			problems.push(`SETTINGS_ONLY ${row.id}: not in SETTINGS_CATALOG`);
		}
		if (typeof row.reason !== 'string' || row.reason.trim() === '') {
			problems.push(`SETTINGS_ONLY ${row.id}: empty reason`);
		}
	}
	for (const [id, count] of bucketIdCounts) {
		if (count > 1) problems.push(`SETTINGS_ONLY ${id}: listed twice`);
	}

	for (const id of implementedIds) {
		const placeNames = idToPlaces.get(id) ?? [];
		const inBucket = bucketIds.has(id);

		if (placeNames.length === 0 && !inBucket) {
			problems.push(`${id}: in no customise screen and not in SETTINGS_ONLY`);
			continue;
		}

		if (placeNames.length > 1) {
			const isPinnedOverlap =
				allowedOverlap.has(id) &&
				placeNames.length === 2 &&
				placeNames.includes('LIBRARY_SCREEN') &&
				placeNames.includes('WAVE_QS_GROUPS');
			if (!isPinnedOverlap) {
				problems.push(`${id}: in two screens (${placeNames.join(', ')})`);
			}
		}

		if (placeNames.length > 0 && inBucket) {
			problems.push(`${id}: in screen ${placeNames[0]} and in SETTINGS_ONLY`);
		}
	}

	return problems.sort();
}

/**
 * @param {Record<string, unknown>} mod
 * @param {Record<string, unknown>} waveQsMod
 */
function buildCatalogCoveragePlaces(mod, waveQsMod) {
	if (mod.WAVEFORM_SCREEN !== undefined) {
		throw new Error(
			'WAVEFORM_SCREEN export exists: remove the WAVE_QS_GROUPS stand-in from the CUSTSCR-10206 coverage test'
		);
	}

	/** @type {Array<{ name: string; ids: string[] }>} */
	const places = [];
	for (const [key, val] of Object.entries(mod)) {
		if (!key.endsWith('_SCREEN')) continue;
		if (!val || typeof val !== 'object' || !('id' in val)) continue;
		places.push({ name: key, ids: mod.customiseSettingIds(val) });
	}

	for (const [, def] of Object.entries(mod).filter(([k]) => k.endsWith('_SCREEN'))) {
		const screenDef = /** @type {{ id: string }} */ (def);
		assert.ok(
			mod.CUSTOMISE_SCREENS.some((s) => s.id === screenDef.id),
			`${screenDef.id}: *_SCREEN export missing from CUSTOMISE_SCREENS`
		);
	}
	for (const s of mod.CUSTOMISE_SCREENS) {
		const hasExport = Object.values(mod).some(
			(v) =>
				v &&
				typeof v === 'object' &&
				'id' in v &&
				/** @type {{ id: string }} */ (v).id === s.id &&
				'groups' in v
		);
		assert.ok(hasExport, `CUSTOMISE_SCREENS entry ${s.id} has no matching *_SCREEN export`);
	}

	// Stands in for WAVEFORM_SCREEN until #9439; drop this place once WAVEFORM_SCREEN lands.
	const waveQsIds = waveQsMod.WAVE_QS_GROUPS.flatMap((g) =>
		g.rows.filter((r) => r.kind === 'setting').map((r) => r.id)
	);
	places.push({ name: 'WAVE_QS_GROUPS', ids: waveQsIds });
	return places;
}

before(async () => {
	customiseScreens = await loadTypeScriptModule('src/lib/settings/customise-screens.ts');
	catalog = await loadTypeScriptModule('src/lib/settings/catalog.ts');
	numberStep = await loadTypeScriptModule('src/lib/settings/setting-number-step.ts');
	waveQs = await loadTypeScriptModule('src/lib/settings/waveform-quick-settings.ts');
	ssr = await loadSvelteSsrModule(
		"export { default as CustomiseScreen } from '$lib/components/rb/customise/CustomiseScreen.svelte';\n" +
			"export { default as SettingNumberStepper } from '$lib/components/settings/SettingNumberStepper.svelte';"
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
	assert.deepEqual(customiseScreens.CUSTOMISE_SCREENS.map((s) => s.id), [
		'decks',
		'library',
		'layout'
	]);
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
	assert.equal(customiseScreens.resolveCustomiseSetting(NUMBER_FAKE, 'decks', 'n').id, 'n');
	assert.equal(waveQs.resolveWaveQsSetting(NUMBER_FAKE, 'n').id, 'n');

	const pathLines = [
		{ id: 'p', implemented: true, control: { kind: 'path_lines', notice: 'x' } }
	];
	const link = [{ id: 'l', implemented: true, control: { kind: 'link', href: '/' } }];
	const multi = [{ id: 'm', implemented: true, control: { kind: 'multi_bool', keys: [] } }];
	for (const fake of [pathLines, link, multi]) {
		const id = fake[0].id;
		assert.throws(
			() => customiseScreens.resolveCustomiseSetting(fake, 'decks', id),
			/only boolean, enum and number/
		);
		assert.throws(() => waveQs.resolveWaveQsSetting(fake, id), /only boolean, enum and number/);
	}
});

test('CUSTSCR-10464 setting-number-step pure helpers', () => {
	const c = STEP_CONTROL;
	const { stepSettingNumber, stepperKeySteps, parseStepperText, formatSettingNumber } = numberStep;
	assert.equal(stepSettingNumber(c, 100, 1), null);
	assert.equal(stepSettingNumber(c, 0, -1), null);
	assert.equal(stepperKeySteps('ArrowUp', false), 1);
	assert.equal(stepperKeySteps('ArrowDown', false), -1);
	assert.equal(stepperKeySteps('ArrowUp', true), 10);
	assert.equal(stepperKeySteps('ArrowDown', true), -10);
	assert.equal(stepperKeySteps('Enter', false), null);
	assert.equal(stepSettingNumber(c, 35, 1), 40);
	assert.equal(stepSettingNumber(c, 35, 10), 85);
	assert.equal(stepSettingNumber(c, 60, 10), 100);
	assert.equal(stepSettingNumber(c, 40, -10), 0);
	const fine = { kind: 'number', min: 0.05, max: 4, step: 0.05, defaultValue: 0.1, unit: '' };
	assert.equal(stepSettingNumber(fine, 0.1, 1), 0.15);
	assert.equal(parseStepperText(c, '35'), 35);
	assert.equal(parseStepperText(c, '35 %'), 35);
	assert.equal(parseStepperText(c, '101'), null);
	assert.equal(parseStepperText(c, '-5'), null);
	assert.equal(parseStepperText(c, 'abc'), null);
	assert.equal(parseStepperText(c, ''), null);
	assert.equal(formatSettingNumber(30, 1), '30');
	assert.equal(formatSettingNumber(1, 0.05), '1.00');
});

test('CUSTSCR-10464 SettingNumberStepper SSR markup', async () => {
	const { render } = await import('svelte/server');
	const control = STEP_CONTROL;
	const html35 = render(ssr.SettingNumberStepper, {
		props: {
			id: 'x',
			label: 'Level',
			title: 't',
			control,
			value: 35,
			onchange: () => {}
		}
	}).body;
	assert.match(html35, /role="spinbutton"/);
	assert.match(html35, /aria-valuenow="35"/);
	assert.match(html35, /value="35"/);
	assert.match(html35, /aria-valuemin="0"/);
	assert.match(html35, /aria-valuemax="100"/);
	assert.match(html35, />%/);
	assert.match(html35, /Decrease Level/);
	assert.match(html35, /Increase Level/);
	const html100 = render(ssr.SettingNumberStepper, {
		props: { id: 'x', label: 'Level', title: 't', control, value: 100, onchange: () => {} }
	}).body;
	assert.match(html100, /Increase Level" disabled/);
	assert.doesNotMatch(html100, /Decrease Level" disabled/);
});

test('CUSTSCR-10464 CustomiseScreen SSR renders a number catalog row as stepper', async () => {
	const { render } = await import('svelte/server');
	const apply = await loadTypeScriptModule('src/lib/settings/apply.ts');
	const falloff = catalog.SETTINGS_CATALOG.find((e) => e.id === 'drag_sensitivity.falloff_px');
	assert.ok(falloff);
	const expected = Number(apply.readSettingValue('drag_sensitivity.falloff_px'));
	const def = {
		id: 'probe',
		label: 'Probe',
		groups: [{ id: 'g', label: 'G', rows: [{ kind: 'setting', id: 'drag_sensitivity.falloff_px' }] }]
	};
	const html = render(ssr.CustomiseScreen, { props: { def } }).body;
	assert.match(html, /data-customise-row="drag_sensitivity\.falloff_px"/);
	assert.match(html, /role="spinbutton"/);
	assert.match(html, /id="customise-probe-drag_sensitivity\.falloff_px"/);
	assert.match(html, /px/);
	assert.match(html, new RegExp(`aria-valuenow="${expected}"`));
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

test('library group catalog coverage (18 entries; two non-boolean/enum omitted)', () => {
	const screen = customiseScreens.customiseScreen('library');
	const rowIds = new Set(allScreenRowIds(screen));

	const lib = catalog.SETTINGS_CATALOG.filter((e) => e.group === 'library');
	assert.equal(lib.length, 18);

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
	assert.equal(ids.length, 17);
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
		/only boolean, enum and number/
	);

	assert.throws(() => customiseScreen('nope'), /unknown screen nope/);
});

// requirement: SKINLAYOUT (issue #10266): the Layout customise screen.
test('layout: every listed key exists in SETTINGS_CATALOG; no key shared with other screens', () => {
	const layoutIds = customiseScreens.customiseSettingIds(
		customiseScreens.customiseScreenDef('layout')
	);
	assert.ok(layoutIds.includes('layout_override.performance'));
	assert.ok(layoutIds.includes('layout_override.library'));
	assert.ok(layoutIds.includes('deck_layout'), 'deck_layout lives on Layout, not Decks');
	assert.ok(!DECKS_IDS_ORDER.includes('deck_layout'));

	for (const id of layoutIds) {
		assert.ok(
			catalog.SETTINGS_CATALOG.find((e) => e.id === id),
			`${id} missing from SETTINGS_CATALOG`
		);
		customiseScreens.resolveCustomiseSetting(catalog.SETTINGS_CATALOG, 'layout', id);
	}

	const otherIds = new Set();
	for (const screen of customiseScreens.CUSTOMISE_SCREENS) {
		if (screen.id === 'layout') continue;
		for (const id of allScreenRowIds(screen)) otherIds.add(id);
	}
	for (const id of layoutIds) {
		assert.ok(!otherIds.has(id), `${id} appears on another customise screen`);
	}

	const screen = customiseScreens.customiseScreen('layout');
	assert.equal(screen.label, 'Layout');
	assert.ok(screen.groups.some((g) => g.id === 'presets'));
	assert.ok(screen.groups.some((g) => g.id === 'modes'));
	const lookPresent = customiseScreens.LAYOUT_LOOK_VARIANT_IDS.some((id) =>
		catalog.SETTINGS_CATALOG.some((e) => e.id === id)
	);
	assert.equal(
		screen.groups.some((g) => g.id === 'look'),
		lookPresent,
		'Look group is listed exactly when a variant key is in the catalog'
	);
});

test('layout: stub catalog missing variant keys still resolves Presets and Modes', () => {
	const stub = catalog.SETTINGS_CATALOG.filter(
		(e) =>
			!customiseScreens.LAYOUT_LOOK_VARIANT_IDS.includes(e.id) &&
			!customiseScreens.LAYOUT_MODE_OPTIONAL_IDS.includes(e.id)
	);
	const def = customiseScreens.layoutScreenDef(stub);
	assert.deepEqual(
		def.groups.map((g) => g.id),
		['presets', 'modes']
	);
	assert.deepEqual(customiseScreens.customiseSettingIds(def), [
		'layout_override.performance',
		'layout_override.library',
		'deck_layout'
	]);
	const [resolved] = customiseScreens.defineCustomiseScreens(stub, [def]);
	assert.equal(resolved.id, 'layout');
	assert.deepEqual(
		resolved.groups.map((g) => g.id),
		['presets', 'modes']
	);
});

test('layout: with look variants in a stub catalog the Look group appears', () => {
	const donor = catalog.SETTINGS_CATALOG.find((e) => e.id === 'deck_layout');
	assert.ok(donor);
	const stub = [
		...catalog.SETTINGS_CATALOG,
		...customiseScreens.LAYOUT_LOOK_VARIANT_IDS.map((id) => ({
			...donor,
			id,
			label: id
		}))
	];
	const def = customiseScreens.layoutScreenDef(stub);
	assert.deepEqual(
		def.groups.map((g) => g.id),
		['presets', 'look', 'modes']
	);
	assert.deepEqual(
		def.groups.find((g) => g.id === 'look').rows.map((r) => r.id),
		[...customiseScreens.LAYOUT_LOOK_VARIANT_IDS]
	);
});

test('CUSTSCR-10206 every implemented catalog id is in exactly one screen or SETTINGS_ONLY', () => {
	const places = buildCatalogCoveragePlaces(customiseScreens, waveQs);
	const problems = coverageProblems(
		catalog.SETTINGS_CATALOG,
		places,
		customiseScreens.SETTINGS_ONLY,
		KNOWN_STANDIN_OVERLAP
	);
	assert.deepEqual(problems, []);
});

test('CUSTSCR-10206 stand-in overlap is pinned', () => {
	const libIds = customiseScreens.customiseSettingIds(customiseScreens.LIBRARY_SCREEN);
	const waveIds = waveQs.WAVE_QS_GROUPS.flatMap((g) =>
		g.rows.filter((r) => r.kind === 'setting').map((r) => r.id)
	);
	const libSet = new Set(libIds);
	const overlap = waveIds.filter((id) => libSet.has(id)).sort();
	assert.deepEqual(overlap, ['lyrics_hover_scrub']);
});

test('CUSTSCR-10206 SETTINGS_ONLY seeds the spike 19 rows', () => {
	const bucketIds = new Set(customiseScreens.SETTINGS_ONLY.map((r) => r.id));
	for (const id of SPIKE_SETTINGS_ONLY_19) {
		assert.ok(bucketIds.has(id), `spike bucket missing ${id}`);
	}
});

test('CUSTSCR-10206 coverage checker negative controls', () => {
	const syntheticCatalog = [
		{ id: 'a', implemented: true },
		{ id: 'b', implemented: true },
		{ id: 'c', implemented: true },
		{ id: 'ghost', implemented: false }
	];

	const missing = coverageProblems(
		syntheticCatalog,
		[{ name: 'A', ids: ['a'] }],
		[{ id: 'c', reason: 'ok' }]
	);
	assert.ok(missing.some((p) => p.includes('b') && p.includes('no customise screen')));

	const twoScreens = coverageProblems(
		syntheticCatalog,
		[
			{ name: 'A', ids: ['a', 'b'] },
			{ name: 'B', ids: ['b'] }
		],
		[]
	);
	assert.ok(
		twoScreens.some((p) => p.includes('b') && p.includes('A') && p.includes('B') && p.includes('two screens'))
	);

	const screenAndBucket = coverageProblems(
		syntheticCatalog,
		[{ name: 'A', ids: ['a'] }],
		[{ id: 'a', reason: 'dup' }]
	);
	assert.ok(
		screenAndBucket.some((p) => p.includes('a') && p.includes('SETTINGS_ONLY') && p.includes('A'))
	);

	const emptyReason = coverageProblems(syntheticCatalog, [], [
		{ id: 'a', reason: '' },
		{ id: 'b', reason: '   ' }
	]);
	assert.ok(emptyReason.some((p) => p.includes('a') && p.includes('empty reason')));
	assert.ok(emptyReason.some((p) => p.includes('b') && p.includes('empty reason')));

	const notInCatalog = coverageProblems(syntheticCatalog, [], [
		{ id: 'no_such_setting', reason: 'nope' }
	]);
	assert.ok(notInCatalog.some((p) => p.includes('no_such_setting') && p.includes('not in SETTINGS_CATALOG')));

	const unimplementedOk = coverageProblems(syntheticCatalog, [{ name: 'A', ids: ['a'] }], []);
	assert.ok(!unimplementedOk.some((p) => p.includes('ghost')));

	const wrongOverlapException = coverageProblems(
		syntheticCatalog,
		[
			{ name: 'A', ids: ['a', 'b'] },
			{ name: 'B', ids: ['b'] }
		],
		[],
		new Set(['b'])
	);
	assert.ok(
		wrongOverlapException.some((p) => p.includes('b') && p.includes('two screens')),
		'allowedOverlap must not waive overlap unless places are LIBRARY_SCREEN and WAVE_QS_GROUPS'
	);

	const clean = coverageProblems(
		syntheticCatalog,
		[
			{ name: 'A', ids: ['a'] },
			{ name: 'B', ids: ['b'] }
		],
		[{ id: 'c', reason: 'settings only' }]
	);
	assert.deepEqual(clean, []);
});
