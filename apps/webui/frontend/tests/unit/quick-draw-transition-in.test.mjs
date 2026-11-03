/**
 * Source-shape for transition-in refresh after the menu PUT (AGENT-51 / issue #7916).
 *
 * Regression lines:
 * - if the transition-in runner PUTs but never calls engine.refreshHotCues then broken
 * - if the success toast is after the refresh then a refresh throw can look like the PUT failed
 * - if engine is not imported from audio-engine.svelte then the refresh bypasses the real ANLZ path
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

function read(rel) {
	return readFileSync(fileURLToPath(new URL(`../../${rel}`, import.meta.url)), 'utf8');
}

const menu = read('src/lib/components/rb/QuickDrawMenu.svelte');

test('transition-in runner PUTs then refreshes deck ANLZ via engine.refreshHotCues', () => {
	assert.match(menu, /engine,\s*getDeckState/);
	const start = menu.indexOf("runners['transition-in']");
	assert.ok(start >= 0, 'transition-in runner missing');
	const end = menu.indexOf("runners[`transition-out-", start);
	assert.ok(end > start, 'transition-in runner slice missing');
	const runner = menu.slice(start, end);
	const putAt = runner.indexOf('setTransitionInCue(');
	const refreshAt = runner.indexOf('engine.refreshHotCues(');
	assert.ok(putAt >= 0, 'setTransitionInCue call missing');
	assert.ok(refreshAt >= 0, 'engine.refreshHotCues call missing');
	assert.ok(putAt < refreshAt, 'refresh must follow the PUT');
	const toastAt = runner.indexOf('Transition-in cue set at bar');
	assert.ok(toastAt >= 0 && toastAt < refreshAt, 'success toast must precede refresh');
});
