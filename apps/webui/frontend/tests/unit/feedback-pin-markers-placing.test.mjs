// requirement: PGCL-7693d
/**
 * Preview pin 2230ee193a61 (Preview 3f979d486e): pin markers draw while
 * placement is armed and hide again once the draft bubble opens, unless the
 * show-pins checkbox is on.
 *
 * Regression lines:
 *  - if an open draft keeps the markers drawn then they cover the comment being written
 *  - if armed placement stops drawing them then there is nothing to place against
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

const layer = await readFile('src/lib/components/rb/FeedbackPinLayer.svelte', 'utf8');

test('markers show for the saved choice, a hover peek or armed placement only', () => {
	const at = layer.indexOf('const pagePins = $derived(');
	assert.notEqual(at, -1);
	const gate = layer.slice(at, layer.indexOf('?', at));
	assert.match(gate, /pinsVisible/);
	assert.match(gate, /pinsPeek/);
	assert.match(gate, /placementArmed/);
	assert.doesNotMatch(gate, /pinDraft|pendingDraft/);
});
