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
	jog_radial: 'jog_radial_waveform'
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
	assert.deepEqual(previewed.sort(), ['wave_palette', 'waveform_design']);
});

test('WAVEQS-8348b resolve refuses a missing or unimplemented entry (negative controls)', () => {
	assert.throws(() => qs.resolveWaveQsSetting(catalog.SETTINGS_CATALOG, 'no_such_setting'), /no Settings catalog entry/);
	const fake = [{ id: 'x', implemented: false, control: { kind: 'boolean' } }];
	assert.throws(() => qs.resolveWaveQsSetting(fake, 'x'), /not implemented/);
	const link = [{ id: 'y', implemented: true, control: { kind: 'link', href: '/' } }];
	assert.throws(() => qs.resolveWaveQsSetting(link, 'y'), /only boolean and enum/);
});

test('WAVEQS-8348b inert rows carry exactly the PARITY-TODO title and every group is non-empty', () => {
	assert.equal(qs.WAVE_QS_INERT_TITLE, 'not implemented - see PARITY-TODO');
	const inert = qs.WAVE_QS_GROUPS.flatMap((g) => g.rows.filter((r) => r.kind === 'inert'));
	assert.ok(inert.some((r) => r.id === 'tempo_lane'), 'the tempo-change lane row is reserved');
	for (const group of qs.WAVE_QS_GROUPS) assert.ok(group.rows.length > 0, `${group.id} is empty`);
	const popover = readFileSync('src/lib/components/rb/WaveQuickSettingsPopover.svelte', 'utf8');
	assert.match(popover, /title=\{WAVE_QS_INERT_TITLE\}/);
	assert.match(popover, /\sdisabled\s+aria-label=\{row\.label\}/);
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
		assert.match(cli, new RegExp(`Verb\\("${verb}", "${verb}"`), `${verb} is not in apps/opendj_cli/catalog.py`);
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

test('WAVEQS-8348/8348d the glyph title is the requirement text verbatim and open focus targets an option, not the header', () => {
	const button = readFileSync('src/lib/components/rb/WaveQuickSettingsButton.svelte', 'utf8');
	assert.match(button, /const TITLE = 'Waveform quick settings';/);
	assert.match(button, /title=\{TITLE\}/);
	assert.doesNotMatch(button, /loadError/, 'a load error must not replace the title');
	const popover = readFileSync('src/lib/components/rb/WaveQuickSettingsPopover.svelte', 'utf8');
	assert.match(popover, /querySelector<HTMLElement>\(\s*'\.wqs-group select, \.wqs-group input:not\(:disabled\)'\s*\)/);
});
