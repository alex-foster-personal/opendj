/**
 * Source-shape for vocal-area Fix / add comment (FB-12 / issue #698).
 *
 * The menu item is a closure inside QuickDrawMenu with no node:test-able
 * seam that can see a live MouseEvent + deck state, so this file reads the
 * sources the same way comment-hotkey.test.mjs does.
 *
 * Regression lines:
 * - if the Fix / add comment label leaves QuickDraw then the right-click
 *   path the maintainer named is gone
 * - if queuePinDraft is not called from the menu item run then choosing
 *   the item cannot open the existing pin draft
 * - if data-wave-surface markers leave WaveRow / StripWaveform then
 *   QuickDraw cannot tell a vocal surface from the rest of the deck
 * - if FeedbackWidget stops consuming takePendingPinDraft then the queued
 *   draft never becomes a bubble
 * - if .qd loses data-testid="quick-draw-menu" then the e2e cannot find it
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

function read(rel) {
	return readFileSync(fileURLToPath(new URL(`../../${rel}`, import.meta.url)), 'utf8');
}

const menu = read('src/lib/components/rb/QuickDrawMenu.svelte');
const helper = read('src/lib/components/rb/vocal-correction-menu.ts');
const waveRow = read('src/lib/components/rb/wave/WaveRow.svelte');
const strip = read('src/lib/components/rb/deck/StripWaveform.svelte');
const widget = read('src/lib/components/rb/FeedbackWidget.svelte');
// #3892: the pin draft bubble now lives in the root-mounted pin layer.
const pinLayer = read('src/lib/components/rb/FeedbackPinLayer.svelte');

test('QuickDraw offers Fix / add comment', () => {
	assert.match(helper, /Fix \/ add comment/);
	assert.match(menu, /vocalFixMenuItem/);
});

test('the menu item run calls queuePinDraft', () => {
	assert.match(helper, /queuePinDraft\(/);
	assert.match(helper, /encodeVocalAnchor\(hit\)/);
	assert.match(helper, /formatVocalComment\(hit\)/);
});

test('wavestack and strip surfaces carry data-wave-surface markers', () => {
	assert.match(waveRow, /data-wave-surface="row"/);
	assert.match(strip, /data-wave-surface="strip"/);
});

test('the pin layer consumes takePendingPinDraft into the existing bubble', () => {
	assert.match(pinLayer, /takePendingPinDraft\(\)/);
});

test('.qd has data-testid="quick-draw-menu" and the item uses quick-draw-vocal-fix', () => {
	assert.match(menu, /data-testid="quick-draw-menu"/);
	assert.match(helper, /quick-draw-vocal-fix/);
	assert.match(menu, /data-testid=\{item\.testId\}/);
});

test('a daemon without feedback disables the item with the same copy as the topbar icon', () => {
	assert.match(
		helper,
		/Comment pins - this daemon does not serve \/api\/v1\/feedback, so dropping a pin is unavailable/
	);
	assert.match(helper, /Comment pins - probing the daemon for \/api\/v1\/feedback/);
	assert.match(widget, /Comment pins - this daemon does not serve \/api\/v1\/feedback, so dropping a pin is unavailable/);
});
