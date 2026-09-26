/**
 * Issue #3982: ControlExplainer SVG demos for MIX, LINK, split view, headphone modes.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const EXPLAINER = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/deck/ControlExplainer.svelte', import.meta.url)),
	'utf8'
);
const TOPBAR = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/TopBar.svelte', import.meta.url)),
	'utf8'
);
const HP = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/mixer/HeadphoneCluster.svelte', import.meta.url)),
	'utf8'
);

test('ControlExplainer declares headphone-mix demo keyframes', () => {
	assert.match(EXPLAINER, /demo === 'headphone-mix'/);
	assert.match(EXPLAINER, /@keyframes mix-knob-turn/);
	assert.match(EXPLAINER, /@keyframes hp-route-dot/);
	assert.match(EXPLAINER, /master-path/);
	assert.match(EXPLAINER, /translate\(28px, -12px\)/, 'route dot must visit master path (y=18) after cue leg');
	assert.match(EXPLAINER, /translate\(28px, 0\)/, 'route dot must travel along cue path before master');
	assert.doesNotMatch(EXPLAINER, /\.master-path[\s\S]*--rb-red/);
	assert.match(EXPLAINER, /demo === 'split-view'/);
	assert.match(EXPLAINER, /demo === 'link'/);
	assert.match(EXPLAINER, /demo === 'headphone-mode'/);
});

test('TopBar split view and LINK use ControlExplainer demos', () => {
	assert.match(TOPBAR, /demo="split-view"/);
	assert.match(TOPBAR, /demo="link"/);
	assert.doesNotMatch(TOPBAR, /title=\{plannedTitle\('split-view'\)\}/);
	assert.doesNotMatch(TOPBAR, /title=\{plannedTitle\('link'\)\}/);
});

test('HeadphoneCluster wires MIX and mode demos', () => {
	assert.match(HP, /demo="headphone-mix"/);
	assert.match(HP, /demo="headphone-mode"/);
	assert.match(HP, /demo="headphone-practice"/);
	assert.match(HP, /demo="headphone-split"/);
});
