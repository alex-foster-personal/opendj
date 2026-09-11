/** The JogDial panel has one Master Tempo control wired through the shared
 * typed dispatcher. A duplicate causes ambiguous real-browser locators. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const jogDial = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/deck/JogDial.svelte', import.meta.url)),
	'utf8'
);
const deck = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/Deck.svelte', import.meta.url)),
	'utf8'
);

test('JogDial exposes one Master Tempo control', () => {
	const controls = jogDial.match(/data-performance-control="master-tempo"/g) ?? [];
	assert.equal(controls.length, 1, 'JogDial renders a duplicate Master Tempo button');
	assert.match(jogDial, /onclick=\{async \(\) => await onMasterTempo\(\)\}/);
	assert.equal(jogDial.includes('rb-lit-button small'), false);
});

test('Master Tempo keeps its shared typed dispatcher', () => {
	assert.match(deck, /type: 'master_tempo'/);
	assert.match(deck, /enabled: !deck\.master_tempo_enabled/);
	assert.match(deck, /onMasterTempo=\{toggleMasterTempo\}/);
});
