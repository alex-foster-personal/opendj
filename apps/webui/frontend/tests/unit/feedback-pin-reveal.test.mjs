/**
 * Issue #3528: arming placement reveals hidden pins before the overlay opens.
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

test('armPinPlacement shows pins for the placement only and does not save visibility on', async () => {
	const store = await readFile('src/lib/rb/feedback-store.svelte.ts', 'utf8');
	const armAt = store.indexOf('export function armPinPlacement');
	assert.notEqual(armAt, -1);
	const armBody = store.slice(armAt, store.indexOf('export function disarmPinPlacement'));
	assert.match(armBody, /feedbackState\.placementArmed = true/);
	assert.doesNotMatch(armBody, /writePinsVisible/, 'arming must not flip the saved show-pins checkbox');
	assert.match(store, /pinsPeek/);
	assert.match(store, /setCommentPinPeek/);

	const layer = await readFile('src/lib/components/rb/FeedbackPinLayer.svelte', 'utf8');
	const pagePinsAt = layer.indexOf('const pagePins = $derived(');
	assert.notEqual(pagePinsAt, -1);
	const pagePinsBody = layer.slice(pagePinsAt, pagePinsAt + 450);
	assert.match(pagePinsBody, /pinsVisible/);
	assert.match(pagePinsBody, /pinsPeek/);
	assert.match(pagePinsBody, /placementArmed/);
	assert.doesNotMatch(
		pagePinsBody,
		/pinDraft|pendingDraft/,
		'markers stay up only while placing, not while the comment is being written'
	);
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
