// issue #10085: Mixer settings group in the settings catalogue (rekordbox Preferences > Mixer parity).

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const REPO = fileURLToPath(new URL('../../../../..', import.meta.url));

/** Expected shape before this PR moved only `group` to `mixer`. */
const MIXER_STEMS_IDENTITY = {
	id: 'mixer_stems',
	label: 'Mixer stem dials (S)',
	keywords: [
		'stem',
		'stems',
		'mixer',
		'vocal',
		'inst',
		'bass',
		'drums',
		'dial',
		'knob',
		's button'
	],
	control: { kind: 'boolean' }
};

const CROSSFADE_CURVE_IDENTITY = {
	id: 'crossfade_curve',
	label: 'Crossfader curve',
	keywords: ['crossfader', 'curve', 'magic', 'xfade'],
	control: {
		kind: 'enum',
		options: [{ value: 'magic', label: 'magic crossfader' }]
	}
};

let catalog;
let search;

before(async () => {
	catalog = await loadTypeScriptModule('src/lib/settings/catalog.ts');
	search = await loadTypeScriptModule('src/lib/settings/search.ts');
});

test('[if] SETTING_GROUPS lists Mixer after Performance [then] the sidebar order matches rekordbox, [else stop].', () => {
	const ids = catalog.SETTING_GROUPS.map((g) => g.id);
	const perf = ids.indexOf('performance');
	const mixer = ids.indexOf('mixer');
	assert.ok(perf >= 0 && mixer >= 0, 'performance and mixer must exist');
	assert.equal(mixer, perf + 1, 'mixer must render immediately after performance');
	const row = catalog.SETTING_GROUPS.find((g) => g.id === 'mixer');
	assert.equal(row?.label, 'Mixer');
	assert.equal(catalog.groupLabel('mixer'), 'Mixer');
});

test('[if] crossfade_curve and mixer_stems only change group to mixer [then] ids keywords and controls stay stable, [else stop].', () => {
	const stems = catalog.SETTINGS_CATALOG.find((d) => d.id === 'mixer_stems');
	const xfade = catalog.SETTINGS_CATALOG.find((d) => d.id === 'crossfade_curve');
	assert.ok(stems && xfade);
	assert.equal(stems.group, 'mixer');
	assert.equal(xfade.group, 'mixer');
	for (const [def, expected] of [
		[stems, MIXER_STEMS_IDENTITY],
		[xfade, CROSSFADE_CURVE_IDENTITY]
	]) {
		assert.equal(def.id, expected.id);
		assert.equal(def.label, expected.label);
		assert.deepEqual(def.keywords, expected.keywords);
		assert.deepEqual(def.control, expected.control);
	}
	const { all: stemHits } = search.filterSettings('stem dials', {
		hideTodo: true,
		group: null,
		showDev: false
	});
	assert.ok(
		stemHits.some((d) => d.id === 'mixer_stems'),
		'search must still find mixer_stems'
	);
});

test('[if] a catalogue row references an unknown group [then] stop; mixer holds exactly two rows on this PR, [else stop].', () => {
	const declared = new Set(catalog.SETTING_GROUPS.map((g) => g.id));
	for (const def of catalog.SETTINGS_CATALOG) {
		assert.ok(declared.has(def.group), `${def.id} uses undeclared group ${def.group}`);
	}
	const mixerIds = catalog.SETTINGS_CATALOG.filter((d) => d.group === 'mixer')
		.map((d) => d.id)
		.sort();
	assert.deepEqual(mixerIds, ['crossfade_curve', 'mixer_stems']);
});

test('[if] the Open DJ CLI lists settings by group [then] Mixer appears; else no CLI group listing exists, [else stop].', () => {
	const catalogPy = readFileSync(join(REPO, 'apps/opendj_cli/catalog.py'), 'utf8');
	const mentionsGroups =
		/SETTING_GROUPS|settings.?groups?|group.?label/i.test(catalogPy) && catalogPy.includes('Mixer');
	if (mentionsGroups) {
		assert.match(catalogPy, /Mixer/, 'CLI settings group listing must name Mixer');
	} else {
		assert.ok(
			!catalogPy.includes('SETTING_GROUPS'),
			'control: opendj_cli/catalog.py does not enumerate settings UI groups'
		);
	}
});
