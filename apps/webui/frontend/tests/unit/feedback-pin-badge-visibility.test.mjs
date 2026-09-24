/**
 * FB-16 criteria 2-3: diagnostic badges hidden unless Option (Alt) is held.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const MARKERS = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/feedback/FeedbackPinMarkers.svelte', import.meta.url)),
	'utf8'
);
const LAYER = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/FeedbackPinLayer.svelte', import.meta.url)),
	'utf8'
);

test('badges render only when optionKeyHeld is true', () => {
	assert.match(MARKERS, /\{#if optionKeyHeld\}/);
	assert.match(MARKERS, /optionKeyHeld\s*=\s*false/);
	assert.match(MARKERS, /fb-pin-badge/);
});

test('badge tooltip text stays on the marker title for hover parity', () => {
	assert.match(MARKERS, /board\.badges\.map\(badgeLabel\)/);
});

test('FeedbackPinLayer tracks Option key via Alt keydown and keyup', () => {
	assert.match(LAYER, /optionKeyHeld/);
	assert.match(LAYER, /e\.key === 'Alt'/);
	assert.match(LAYER, /onOptionDown/);
	assert.match(LAYER, /onOptionUp/);
});
