// requirement: LATENCY-01
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { engineBlockAfter, readFrontendSource as readSource } from './engine-source.mjs';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));

function componentSource(relativePath) {
	const text = readFileSync(`${SRC}/${relativePath}`, 'utf8');
	assert.ok(text.trim().length > 0, `if ${relativePath} reads empty then this guard silently asserts nothing`);
	return text;
}

const MIXER_STATE_SETTERS = [
	{ name: 'setEq', anchor: 'setEq(deck: DeckId, band: EqBand, value: number, pressT0Ms?: number): void {' },
	{ name: 'setFilter', anchor: 'setFilter(deck: DeckId, value: number, pressT0Ms?: number): void {' },
	{ name: 'setFader', anchor: 'setFader(deck: DeckId, value: number, pressT0Ms?: number): void {' },
	{ name: 'setCrossfader', anchor: 'setCrossfader(value: number, pressT0Ms?: number): void {' }
];

for (const { name, anchor } of MIXER_STATE_SETTERS) {
	test(`${name} writes mixerState in the same synchronous turn`, () => {
		const body = engineBlockAfter(anchor);
		const stateWrite = body.indexOf('mixerState');
		assert.notEqual(stateWrite, -1, `${name} must write mixerState before the AudioParam call`);
		assert.equal(body.indexOf('await '), -1, `${name} must not await on the Class A path`);
	});
}

test('setEq does not call _setParam', () => {
	const body = engineBlockAfter('setEq(deck: DeckId, band: EqBand, value: number, pressT0Ms?: number): void {');
	assert.doesNotMatch(body, /_setParam/);
});

test('applyStemControl writes controls before setControls', () => {
	const stem = readSource('src/lib/rb/stem-engine-controls.ts');
	const controlsWrite = stem.indexOf('st.stems.controls = controls;');
	const setControls = stem.indexOf('rt.processor.setControls(controls);');
	assert.notEqual(controlsWrite, -1);
	assert.notEqual(setControls, -1);
	assert.ok(controlsWrite < setControls);
});

test('StemRow chips are not disabled by deck pending', () => {
	const row = componentSource('lib/components/rb/deck/StemRow.svelte');
	assert.match(row, /disabled=\{!ready \|\| unavailable\(stem\.id\)\}/);
	assert.doesNotMatch(row, /disabled=\{[^}]*pending[^}]*\}/);
	assert.match(row, /aria-busy=\{pending\}/);
});

test('mixer knobs and faders do not disable on pending', () => {
	for (const path of [
		'lib/components/rb/mixer/ChannelStrip.svelte',
		'lib/components/rb/mixer/Crossfader.svelte',
		'lib/components/rb/mixer/Knob.svelte'
	]) {
		const src = componentSource(path);
		assert.doesNotMatch(src, /disabled=\{[^}]*pending[^}]*\}/, `${path} must not disable Class A controls on pending`);
	}
});

test('Mixer stem handlers fire without awaiting', () => {
	const mixer = componentSource('lib/components/rb/Mixer.svelte');
	assert.match(mixer, /void runPerformanceCommandFromUi\(\{ type: 'stem_mute'/);
	assert.match(mixer, /void runPerformanceCommandFromUi\(\{ type: 'stem_solo'/);
	assert.doesNotMatch(mixer, /await runPerformanceCommandFromUi\(\{ type: 'stem_mute'/);
	assert.doesNotMatch(mixer, /await runPerformanceCommandFromUi\(\{ type: 'stem_solo'/);
});

test('immediate hot_cue_trigger forwards pressT0Ms and avoids SYNC_SCHEDULE_SAFETY_S', () => {
	const ipc = readSource('src/lib/rb/performance-ipc.svelte.ts');
	const executeStart = ipc.indexOf('async function _execute(command: PerformanceCommand, pressT0Ms?: number): Promise<void> {');
	assert.notEqual(executeStart, -1);
	const executeBody = ipc.slice(executeStart);
	assert.ok(
		/plan\.kind === 'immediate'[\s\S]*jump\([^)]*pressT0Ms/.test(executeBody),
		'immediate hot_cue_trigger must forward pressT0Ms into jump'
	);
	assert.doesNotMatch(executeBody, /SYNC_SCHEDULE_SAFETY_S/);
});
