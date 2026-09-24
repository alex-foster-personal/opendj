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

test('FeedbackPinLayer keeps pagePins gated on pinsVisible for marker rendering', async () => {
	// FB-16 (main, merged Thu 24 Sep 2026) moved the pinsVisible-gated pin
	// rendering out of FeedbackWidget.svelte into a dedicated root-mounted
	// FeedbackPinLayer.svelte; re-pointed here rather than at the old file
	// after resolving the merge conflict in performance-hotkeys.ts pulled
	// that refactor onto this branch.
	const layer = await readFile('src/lib/components/rb/FeedbackPinLayer.svelte', 'utf8');
	assert.match(layer, /const pagePins = \$derived\(\s*pinsVisible/);
	assert.match(layer, /<FeedbackPinMarkers\s*\n?\s*pins=\{pagePins\}/);
});
