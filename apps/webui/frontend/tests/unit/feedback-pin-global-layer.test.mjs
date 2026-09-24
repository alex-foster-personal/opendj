/**
 * FB-16 criterion 6: global pin layer and shell affordance on every route.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const LAYOUT = readFileSync(
	fileURLToPath(new URL('../../src/routes/+layout.svelte', import.meta.url)),
	'utf8'
);
const LAYER = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/FeedbackPinLayer.svelte', import.meta.url)),
	'utf8'
);

test('+layout.svelte mounts FeedbackPinLayer outside the performance-only branch', () => {
	const perfBranch = LAYOUT.indexOf('{#if isPerformance}');
	const layerAt = LAYOUT.indexOf('<FeedbackPinLayer');
	assert.ok(layerAt > perfBranch, 'layer must not live only inside isPerformance');
	assert.match(LAYOUT, /<FeedbackPinLayer\s*\/>/);
});

test('app-shell topbar exposes a comment-pin shell button beside UserBauble', () => {
	assert.match(LAYOUT, /<FeedbackPinShellButton\s*\/>/);
	const topbar = LAYOUT.slice(LAYOUT.indexOf('<div class="topbar">'), LAYOUT.indexOf('</div>', LAYOUT.indexOf('<div class="content">')));
	assert.match(topbar, /FeedbackPinShellButton[\s\S]*UserBauble/);
});

test('FeedbackPinLayer owns placement overlay and resolvePinAnchorAt', () => {
	assert.match(LAYER, /resolvePinAnchorAt/);
	assert.match(LAYER, /fb-place-overlay/);
	assert.match(LAYER, /onpointerdowncapture=\{handlePlacementPointerDown\}/);
});

test('FeedbackPinLayer placement stack sits above root overlays', () => {
	assert.match(LAYER, /OVERLAY_Z_INDEX\.feedbackPinPlacement/);
	assert.match(LAYER, /style:z-index=\{OVERLAY_Z_INDEX\.feedbackPinPlacement\}/);
});

test('FeedbackPinLayer exposes floating affordance while root overlays are open', () => {
	assert.match(LAYER, /anyRootOverlayOpen/);
	assert.match(LAYER, /fb-pin-affordance-float/);
	assert.match(LAYER, /showFloatingAffordance/);
	assert.match(LAYER, /FeedbackPinShellButton/);
});

test('global m hotkey installer is registered from +layout onMount', () => {
	assert.match(LAYOUT, /installCommentPinHotkeys/);
});
