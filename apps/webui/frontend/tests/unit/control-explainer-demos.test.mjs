/**
 * Issue #3982: ControlExplainer SVG demos for MIX, LINK, split view, headphone modes.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { EXPLAINER_DEMO_ANIMATIONS } from '../../src/lib/rb/explainer-demo-keyframes.ts';
import {
	assertKeyframesSync,
	extractStyleBlock,
	parseKeyframesFromStyle
} from './explainer-keyframes-parse.mjs';

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

test('explainer-demo-keyframes.ts imports under node --experimental-strip-types', () => {
	assert.ok(typeof EXPLAINER_DEMO_ANIMATIONS === 'object');
	assert.ok(EXPLAINER_DEMO_ANIMATIONS['cue-return']?.length > 0);
});

test('ControlExplainer demo @keyframes match declared catalogue and use transform/opacity only', () => {
	const styleText = extractStyleBlock(EXPLAINER);
	const parsed = parseKeyframesFromStyle(styleText);
	assertKeyframesSync(EXPLAINER_DEMO_ANIMATIONS, parsed);
});

test('ControlExplainer declares headphone-mix and layout demo branches', () => {
	assert.match(EXPLAINER, /demo === 'headphone-mix'/);
	assert.match(EXPLAINER, /master-path/);
	assert.match(EXPLAINER, /translate\(28px, -12px\)/, 'route dot must visit master path (y=18) after cue leg');
	assert.match(EXPLAINER, /translate\(28px, 0\)/, 'route dot must travel along cue path before master');
	assert.doesNotMatch(EXPLAINER, /\.master-path[\s\S]*--rb-red/);
	assert.match(EXPLAINER, /demo === 'split-view'/);
	assert.match(EXPLAINER, /demo === 'link'/);
	assert.match(EXPLAINER, /demo === 'fx'/);
	assert.match(EXPLAINER, /demo === '2-deck-view'/);
	assert.match(EXPLAINER, /demo === 'headphone-mode'/);
});

test('TopBar icon cluster and LINK use ControlExplainer demos', () => {
	// Split view and grid view are unbuilt and HIDDEN for V1 (JIK, Thu 1 Oct
	// 2026), so the topbar no longer renders their buttons or explainers.
	assert.doesNotMatch(TOPBAR, /aria-label="split view"/);
	assert.doesNotMatch(TOPBAR, /aria-label="grid view"/);
	assert.match(TOPBAR, /demo="link"/);
	assert.match(TOPBAR, /demo="fx"/);
	assert.match(TOPBAR, /demo="2-deck-view"/);
	assert.match(TOPBAR, /listViewBullets = plannedExplainerBullets\('list-view'\)/);
	assert.match(TOPBAR, /fourWaveformBullets = plannedExplainerBullets\('4-waveform-view'\)/);
	assert.match(TOPBAR, /scopeView1Bullets = plannedExplainerBullets\('scope-view-1'\)/);
	assert.doesNotMatch(TOPBAR, /title=\{plannedTitle\('split-view'\)\}/);
	assert.doesNotMatch(TOPBAR, /title=\{plannedTitle\('link'\)\}/);
	assert.doesNotMatch(TOPBAR, /title=\{plannedTitle\('fx'\)\}/);
	assert.doesNotMatch(TOPBAR, /title=\{plannedTitle\('2-deck-view'\)\}/);
});

test('HeadphoneCluster wires MIX and mode demos', () => {
	assert.match(HP, /demo="headphone-mix"/);
	assert.match(HP, /demo="headphone-mode"/);
	assert.match(HP, /demo="headphone-practice"/);
	assert.match(HP, /demo="headphone-split"/);
});
