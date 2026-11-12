// requirement: WAVEQS-11736
// [if] the bar lines row stays inert, its title omits the CLI verb, or the CLI drops a grid value [then] this fails, [else stop].
// requirement: CUSTSCR-10464
// [if] the popover lacks a number row branch with SettingNumberStepper [then] this fails, [else stop].
// requirement: WAVEQS-11730
// [if] hot, memory or loop marker rows stay inert or lack a verb [then] this fails, [else stop].
// requirement: WAVEQS-8348b
// [if] a quick-settings row names no Settings catalog entry, or one the Settings path refuses [then] this fails, [else stop].
// [if] an inert row carries any title but the PARITY-TODO one [then] this fails, [else stop].
// [if] the popover or Settings renders a preview other than through SettingPreview [then] this fails, [else stop].
// [if] a live row has no command-bus verb, or the overlay verb domain drifts from the CLI [then] this fails, [else stop].
// [if] the glyph shows while the daemon has not declared waveform.quick_settings on [then] this fails, [else stop].
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let qs;
let catalog;
let flag;
let overlay;

before(async () => {
	qs = await loadTypeScriptModule('src/lib/settings/waveform-quick-settings.ts');
	catalog = await loadTypeScriptModule('src/lib/settings/catalog.ts');
	flag = await loadTypeScriptModule('src/lib/rb/waveform-quick-settings-flag.ts');
	overlay = await loadTypeScriptModule('src/lib/rb/waveform-overlay-command.ts');
});

/** Every live row's setting id, in display order. */
function settingIds() {
	return qs.WAVE_QS_GROUPS.flatMap((g) => g.rows.filter((r) => r.kind === 'setting').map((r) => r.id));
}

/** The ui-prefs key each set_waveform_overlay overlay must write (the Settings row it twins). */
const OVERLAY_PREF = {
	sections: 'show_sections',
	lyrics: 'lyrics_waveform_overlay',
	lyrics_scrub: 'lyrics_hover_scrub',
	jog_radial: 'jog_radial_waveform',
	hot_cues: 'show_hot_cue_markers',
	memory_cues: 'show_memory_cue_markers',
	loop_cues: 'show_loop_markers',
	vocal_lane: 'show_vocal_lane',
	grid_lines: 'wave_grid_lines'
};

/** ALLOWED_SETTING_KEYS read out of apply.ts source (the module itself needs the rune singleton). */
function allowedSettingKeys() {
	const src = readFileSync('src/lib/settings/apply.ts', 'utf8');
	const block = src.match(/export const ALLOWED_SETTING_KEYS = \[([\s\S]*?)\] as const;/);
	assert.ok(block, 'could not read ALLOWED_SETTING_KEYS out of apply.ts');
	return new Set([...block[1].matchAll(/'([^']+)'/g)].map((m) => m[1]));
}

test('WAVEQS-8348b every live row resolves to an implemented boolean or enum catalog entry the Settings path accepts', () => {
	const ids = settingIds();
	assert.ok(ids.length >= 10, `expected the full option set, got ${ids.length}`);
	const allowed = allowedSettingKeys();
	for (const id of ids) {
		const def = qs.resolveWaveQsSetting(catalog.SETTINGS_CATALOG, id);
		assert.equal(def.id, id);
		assert.ok(allowed.has(id), `${id} is not an applySettingChange key`);
	}
	// The two rows Settings shows a live preview for carry it here too.
	const previewed = ids.filter((id) => qs.resolveWaveQsSetting(catalog.SETTINGS_CATALOG, id).preview === 'waveform');
	assert.deepEqual(previewed.sort(), ['wave_grid_lines', 'wave_height', 'wave_palette', 'waveform_design']);
});

test('WAVEQS-11733 the half or full height row is live and names opendj set_waveform_design', () => {
	const look = qs.WAVE_QS_GROUPS.find((g) => g.id === 'look');
	assert.ok(look);
	assert.ok(look.rows.some((r) => r.kind === 'setting' && r.id === 'wave_height'));
	assert.ok(!look.rows.some((r) => r.id === 'wave_height' && r.kind === 'inert'));
	const def = qs.resolveWaveQsSetting(catalog.SETTINGS_CATALOG, 'wave_height');
	assert.equal(def.control.kind, 'enum');
	assert.deepEqual(
		def.control.options.map((o) => o.value),
		['full', 'half']
	);
	assert.match(qs.waveQsRowTitle(def), /Agents: opendj set_waveform_design$/);
	const allowed = allowedSettingKeys();
	assert.ok(allowed.has('wave_height'));
});

test('WAVEQS-8348b resolve refuses a missing or unimplemented entry (negative controls)', () => {
	assert.throws(() => qs.resolveWaveQsSetting(catalog.SETTINGS_CATALOG, 'no_such_setting'), /no Settings catalog entry/);
	const fake = [{ id: 'x', implemented: false, control: { kind: 'boolean' } }];
	assert.throws(() => qs.resolveWaveQsSetting(fake, 'x'), /not implemented/);
	const link = [{ id: 'y', implemented: true, control: { kind: 'link', href: '/' } }];
	assert.throws(() => qs.resolveWaveQsSetting(link, 'y'), /only boolean, enum and number/);
	const numberFake = [
		{
			id: 'n',
			label: 'N',
			title: 'N',
			detail: 'N',
			implemented: true,
			control: { kind: 'number', min: 0, max: 100, step: 5, defaultValue: 50, unit: ' %' }
		}
	];
	assert.equal(qs.resolveWaveQsSetting(numberFake, 'n').id, 'n');
});

test('CUSTSCR-10464 popover renders number rows through SettingNumberStepper', () => {
	const popover = readFileSync('src/lib/components/rb/WaveQuickSettingsPopover.svelte', 'utf8');
	assert.match(popover, /import SettingNumberStepper from/);
	assert.match(popover, /\{:else if def\.control\.kind === 'number'\}/);
	assert.match(popover, /applySettingChange\(def\.id, String\(next\)\)/);
});

test('WAVEQS-8348b inert rows carry exactly the PARITY-TODO title and every group is non-empty', () => {
	assert.equal(qs.WAVE_QS_INERT_TITLE, 'not implemented - see PARITY-TODO');
	const inert = qs.WAVE_QS_GROUPS.flatMap((g) => g.rows.filter((r) => r.kind === 'inert'));
	assert.ok(inert.some((r) => r.id === 'tempo_lane'), 'the tempo-change lane row is reserved');
	const tempoDisplay = qs.resolveWaveQsInertDisplay(catalog.SETTINGS_CATALOG, 'tempo_lane');
	// WAVEQS-10121: the reserved row points at the shipped Settings controls, never claims the lane is unbuilt.
	assert.doesNotMatch(tempoDisplay.detail, /being built|own branch|not built/i);
	const tempoCatalog = catalog.SETTINGS_CATALOG.filter((d) => d.id === 'show_tempo_lane' || d.id === 'tempo_lane_scale');
	assert.equal(tempoCatalog.length, 2, 'both tempo-lane settings exist in the catalog');
	for (const def of tempoCatalog) assert.ok(tempoDisplay.detail.includes(def.label), `the row names ${def.label}`);
	for (const group of qs.WAVE_QS_GROUPS) assert.ok(group.rows.length > 0, `${group.id} is empty`);
	const popover = readFileSync('src/lib/components/rb/WaveQuickSettingsPopover.svelte', 'utf8');
	assert.match(popover, /title=\{WAVE_QS_INERT_TITLE\}/);
	assert.match(popover, /\sdisabled\s+aria-label=\{display\.label\}/);
});

test('WAVEQS-8348b issue #12518 every row label and detail comes from the catalog resolver', () => {
	for (const id of settingIds()) {
		const def = qs.resolveWaveQsSetting(catalog.SETTINGS_CATALOG, id);
		const catalogDef = catalog.SETTINGS_CATALOG.find((d) => d.id === id);
		assert.ok(catalogDef, `${id} missing from SETTINGS_CATALOG`);
		assert.equal(def.label, catalogDef.label);
		assert.equal(def.detail, catalogDef.detail);
	}
	for (const row of qs.WAVE_QS_GROUPS.flatMap((g) => g.rows.filter((r) => r.kind === 'inert'))) {
		const display = qs.resolveWaveQsInertDisplay(catalog.SETTINGS_CATALOG, row.id);
		if (row.id === 'phrase_markers') {
			const stub = qs.WAVE_QS_INERT_DEFS.find((d) => d.id === 'phrase_markers');
			assert.ok(stub);
			assert.equal(display.label, stub.label);
			assert.equal(display.detail, stub.detail);
		}
		assert.ok(display.label.length > 0);
		assert.ok(display.detail.length > 0);
	}
});

test('WAVEQS-8348c Settings and the popover both render previews through the one SettingPreview component', () => {
	const shared = readFileSync('src/lib/components/settings/SettingPreview.svelte', 'utf8');
	assert.match(shared, /import WaveformDesignPreview from '\.\/WaveformDesignPreview\.svelte';/);
	for (const file of ['src/lib/components/settings/SettingsOverlay.svelte', 'src/lib/components/rb/WaveQuickSettingsPopover.svelte']) {
		const src = readFileSync(file, 'utf8');
		assert.match(src, /import SettingPreview from '[^']*SettingPreview\.svelte';/, `${file} must import SettingPreview`);
		assert.match(src, /<SettingPreview kind=\{def\.preview\} label=\{`\$\{def\.label\} preview`\} \/>/, `${file} must render the row's declared preview, named by its row`);
		assert.doesNotMatch(src, /WaveformDesignPreview/, `${file} must not render the painter directly`);
	}
});

test('WAVEQS-8348c the popover writes through applySettingChange and reads through readSettingValue, never uiPrefs directly', () => {
	const src = readFileSync('src/lib/components/rb/WaveQuickSettingsPopover.svelte', 'utf8');
	assert.match(src, /applySettingChange\(def\.id/);
	assert.match(src, /readSettingValue\(def\.id\)/);
	assert.doesNotMatch(src, /uiPrefs/);
});

test('WAVEQS-8348e every live row has a command-bus verb that exists in the CLI catalog', () => {
	const cli = readFileSync('../../opendj_cli/catalog.py', 'utf8');
	for (const id of settingIds()) {
		const verb = qs.WAVE_QS_VERBS[id];
		assert.ok(verb, `${id} has no verb`);
		assert.match(
			cli,
			new RegExp(`Verb\\(\\s*"${verb}"\\s*,\\s*"${verb}"`),
			`${verb} is not in apps/opendj_cli/catalog.py`
		);
	}
	const values = cli.match(/WAVEFORM_OVERLAY_VALUES: tuple\[str, \.\.\.\] = \(([^)]*)\)/);
	assert.ok(values, 'WAVEFORM_OVERLAY_VALUES missing from the CLI catalog');
	assert.deepEqual([...values[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]), [...overlay.WAVEFORM_OVERLAYS]);
	for (const name of overlay.WAVEFORM_OVERLAYS) {
		assert.equal(qs.WAVE_QS_VERBS[OVERLAY_PREF[name]], 'set_waveform_overlay');
	}
	assert.throws(() => overlay.parseWaveformOverlay('beats'), /overlay must be/);
	const def = qs.resolveWaveQsSetting(catalog.SETTINGS_CATALOG, 'show_sections');
	assert.match(qs.waveQsRowTitle(def), /Agents: opendj set_waveform_overlay$/);
	assert.throws(() => qs.waveQsRowTitle({ ...def, id: 'unmapped' }), /no command-bus verb/);
});

test('WAVEQS-8348 the glyph shows only when the daemon declares waveform.quick_settings on', () => {
	const full = JSON.parse(readFileSync('tests/unit/fixtures/flags-full.json', 'utf8'));
	assert.equal(flag.waveQsEnabledIn(full.flags), true);
	assert.equal(flag.waveQsEnabledIn(full.flags.filter((f) => f.flag_id !== 'waveform.quick_settings')), false);
	assert.equal(flag.waveQsEnabledIn([{ flag_id: 'waveform.quick_settings', enabled: false }]), false);
	assert.equal(flag.waveQsEnabledIn([]), false, 'flags still loading show no glyph');
});

// requirement: WAVEQS-11734
// [if] show_vocal_lane is inert or missing from overlays [then] this fails, [else stop].
test('WAVEQS-11734 show_vocal_lane is a live markers row with set_waveform_overlay vocal_lane parity', () => {
	const markers = qs.WAVE_QS_GROUPS.find((g) => g.id === 'markers');
	assert.ok(markers);
	const row = markers.rows.find((r) => r.kind === 'setting' && r.id === 'show_vocal_lane');
	assert.ok(row, 'show_vocal_lane must be a live setting row in markers');
	const def = qs.resolveWaveQsSetting(catalog.SETTINGS_CATALOG, 'show_vocal_lane');
	assert.equal(def.control.kind, 'boolean');
	assert.equal(def.group, 'performance');
	assert.equal(def.implemented, true);
	assert.match(qs.waveQsRowTitle(def), /Agents: opendj set_waveform_overlay$/);
	const inertVocal = qs.WAVE_QS_GROUPS.flatMap((g) => g.rows).find((r) => r.kind === 'inert' && r.id === 'vocal_lane');
	assert.equal(inertVocal, undefined, 'vocal_lane must not remain an inert row');
});

test('WAVEQS-8348/8348d the glyph title is the requirement text verbatim and open focus targets an option, not the header', () => {
	const button = readFileSync('src/lib/components/rb/WaveQuickSettingsButton.svelte', 'utf8');
	assert.match(button, /const TITLE = 'Waveform quick settings';/);
	assert.match(button, /title=\{TITLE\}/);
	assert.doesNotMatch(button, /loadError/, 'a load error must not replace the title');
	const popover = readFileSync('src/lib/components/rb/WaveQuickSettingsPopover.svelte', 'utf8');
	assert.match(popover, /querySelector<HTMLElement>\(\s*'\.wqs-group select, \.wqs-group input:not\(:disabled\)'\s*\)/);
});

test('WAVEQS-11736 the bar lines row is live, its title names the CLI verb, and the CLI declares the three values', async () => {
	const grid = await loadTypeScriptModule('src/lib/rb/wave-grid-lines.ts');
	const rows = qs.WAVE_QS_GROUPS.flatMap((g) => g.rows);
	assert.ok(!rows.some((r) => r.id === 'bar_lines'), 'the inert bar_lines row is gone');
	const markers = qs.WAVE_QS_GROUPS.find((g) => g.id === 'markers');
	assert.ok(markers.rows.some((r) => r.kind === 'setting' && r.id === 'wave_grid_lines'));
	const def = qs.resolveWaveQsSetting(catalog.SETTINGS_CATALOG, 'wave_grid_lines');
	assert.match(qs.waveQsRowTitle(def), /Agents: opendj set_waveform_overlay$/);
	assert.equal(def.control.kind, 'enum');
	assert.deepEqual(def.control.options.map((o) => o.value), ['beats_bars', 'bars', 'off']);
	const cli = readFileSync('../../opendj_cli/catalog.py', 'utf8');
	const values = cli.match(/WAVE_GRID_LINES_VALUES: tuple\[str, \.\.\.\] = \(([^)]*)\)/);
	assert.ok(values, 'WAVE_GRID_LINES_VALUES missing from the CLI catalog');
	assert.deepEqual([...values[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]), [...grid.WAVE_GRID_LINES_VALUES]);
});
