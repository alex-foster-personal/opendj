/**
 * Issue #3528: arming placement reveals hidden pins before the overlay opens.
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

test('armPinPlacement reveals hidden pins through the shared visibility twin', async () => {
	const store = await readFile('src/lib/rb/feedback-store.svelte.ts', 'utf8');
	assert.match(store, /writePinsVisible\(window\.localStorage, true\)/);
	assert.match(store, /__mdtPinsVisible/);
	assert.match(store, /feedbackState\.placementArmed = true/);
});

test('FeedbackWidget keeps pagePins gated on pinsVisible for marker rendering', async () => {
	const widget = await readFile('src/lib/components/rb/FeedbackWidget.svelte', 'utf8');
	assert.match(widget, /const pagePins = \$derived\(\s*pinsVisible/);
	assert.match(widget, /<FeedbackPinMarkers pins=\{pagePins\}/);
});
