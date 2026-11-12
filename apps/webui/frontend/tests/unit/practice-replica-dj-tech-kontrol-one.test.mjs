// requirement: SKIN-10860
// The DJ-Tech Kontrol One practice panel joins DJ_TECH_KONTROL_ONE_MAP (Mixxx hints only, Path B).
//
//   if any panel wire is neither bound nor hinted then broken
//   if play binds deck_play_toggle instead of staying inert then broken
//   if any pad knob or jog binds instead of inert then broken
//   if nameMatch hits Traktor Kontrol X1 or Kontrol Dj Midi In then broken
//   if a bogus wire does not throw on resolveControl then broken

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const PANEL = resolve(
	FRONTEND_ROOT,
	'../../../tools/deck-diagrams/devices/dj-tech-kontrol-one/panel.json'
);

let vite;
let bindings;
let devices;
let DJ_TECH_KONTROL_ONE_MAP;
const layout = JSON.parse(readFileSync(PANEL, 'utf8'));

before(async () => {
	vite = await createServer({
		root: FRONTEND_ROOT,
		configFile: false,
		appType: 'custom',
		logLevel: 'silent',
		server: { middlewareMode: true, watch: null },
		plugins: [svelte()],
		resolve: { alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') }, conditions: ['browser'] }
	});
	bindings = await vite.ssrLoadModule('/src/lib/components/rb/practice/practice-bindings.ts');
	devices = await vite.ssrLoadModule('/src/lib/components/rb/practice/practice-devices.ts');
	({ DJ_TECH_KONTROL_ONE_MAP } = await vite.ssrLoadModule(
		'/src/lib/rb/midi/maps/dj-tech-kontrol-one.ts'
	));
});

after(async () => {
	await vite.close();
});

const byId = (id, shift = false) => {
	const control = layout.controls.find((c) => c.id === id);
	assert.ok(control, `panel has ${id}`);
	return bindings.resolveControl(DJ_TECH_KONTROL_ONE_MAP, control, shift);
};

test('practiceMapFor serves DJ_TECH_KONTROL_ONE_MAP', () => {
	assert.equal(devices.practiceMapFor(layout), DJ_TECH_KONTROL_ONE_MAP);
});

test('every panel control resolves on both layers', () => {
	const resolved = bindings.resolveLayout(DJ_TECH_KONTROL_ONE_MAP, layout);
	assert.equal(resolved.length, layout.controls.length);
	for (const control of layout.controls) {
		bindings.resolveControl(DJ_TECH_KONTROL_ONE_MAP, control, true);
	}
});

test('all wired controls stay inert; deck-select unwired', () => {
	for (const id of ['play', 'cue', 'sync', 'shift', 'knob-1', 'pad-1', 'jog']) {
		assert.equal(byId(id).kind, 'inert', id);
	}
	assert.equal(byId('deck-select').kind, 'unwired');
});

test('nameMatch picks Kontrol One and not siblings', () => {
	const re = new RegExp(DJ_TECH_KONTROL_ONE_MAP.nameMatch, 'i');
	assert.ok(re.test('DJ-Tech Kontrol One'));
	assert.ok(re.test('Kontrol 1'));
	assert.ok(!re.test('Traktor Kontrol X1'));
	assert.ok(!re.test('Traktor Kontrol Z1'));
	assert.ok(!re.test('Kontrol Dj Midi In'));
});

test('unknown wire throws', () => {
	const bogus = {
		...layout.controls[0],
		midi: { ch: 1, kind: 'note', id: 0x7f }
	};
	assert.throws(() => bindings.resolveControl(DJ_TECH_KONTROL_ONE_MAP, bogus, false));
});
