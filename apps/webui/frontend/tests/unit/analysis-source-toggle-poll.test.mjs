/**
 * PARITY-02 rbx-vs-own source toggle (AnalysisSourceToggle.svelte): the fix
 * for discussion_r3921666947 - NON-BLOCKING "Keep the UI synchronized with
 * agent-side PUTs".
 *
 * Before this, the component's only call to loadAnalysisSource() was a
 * one-shot onMount hook: an agent driving PUT /api/v1/analysis-source
 * directly after the page had mounted left analysisSourceState (and the
 * visible RBX/OWN control) stuck at the pre-agent value indefinitely.
 *
 * SOURCE-LEVEL, DELIBERATELY: this repo's test harness (load-typescript.mjs)
 * bundles plain .ts/.svelte.ts modules with esbuild; it has no Svelte
 * compiler plugin, so a .svelte file's <script> block cannot be imported and
 * exercised behaviorally the way analysis-source.svelte.ts's own tests run
 * it (same tradeoff transport-onset-lead.test.mjs and
 * engine-tick-exception-safety.test.mjs already accept for audio-engine.svelte.ts).
 * This is a drift guard on the shape of the code, not a behavioral proof,
 * and should be replaced by a real interaction test if this component ever
 * gets a render harness.
 *
 * Regression lines:
 * - if the component never re-calls loadAnalysisSource after mount then an
 *   agent-driven PUT never reaches the visible toggle
 * - if a poll timer is added with no matching clearInterval cleanup then
 *   broken (a leaked timer per mount, compounding across route navigations)
 * - if the source menu is positioned inside the overflow-hidden top bar then
 *   broken (the RBX and OWN controls are clipped and cannot be selected)
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const SOURCE_PATH = fileURLToPath(
	new URL('../../src/lib/components/rb/AnalysisSourceToggle.svelte', import.meta.url)
);

function _source() {
	const text = readFileSync(SOURCE_PATH, 'utf8');
	assert.ok(text.trim().length > 0, 'if AnalysisSourceToggle.svelte reads empty this guard asserts nothing');
	return text;
}

test('polls loadAnalysisSource on an interval, not just once on mount', () => {
	const text = _source();
	const pollMatches = [...text.matchAll(/setInterval\([\s\S]{0,60}?loadAnalysisSource/g)];
	assert.equal(
		pollMatches.length,
		1,
		'expected exactly one setInterval wired to loadAnalysisSource - none means an agent-side PUT ' +
			'never reaches this control, more than one is unexplained duplication'
	);
});

test('the poll timer is cleared, so it does not outlive the component', () => {
	const text = _source();
	assert.match(
		text,
		/clearInterval\(/,
		'a setInterval with no matching clearInterval leaks a timer per mount'
	);
});

test('the source menu is viewport-positioned outside the clipped top bar', () => {
	const text = _source();
	assert.match(text, /getBoundingClientRect\(\)/, 'menu placement must measure its trigger');
	assert.match(text, /style=\{menuStyle\}/, 'the measured viewport placement must reach the menu');
	assert.match(
		text,
		/\.src-menu\s*\{\s*position:\s*fixed/s,
		'a menu below an overflow-hidden top bar must use fixed positioning'
	);
});
