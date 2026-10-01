/**
 * The marker layer draws each pin where FeedbackPinLayer resolved it
 * (feedback-pin-position.ts) and says so in its DOM, which is the agent's
 * query surface alongside window.__mdtPinPositions.
 *
 * Regression lines:
 * - if a marker with no resolved position stops drawing at its stored
 *   percentages, old pins moved
 * - if a pin re-placed on a nearby anchor loses its data-pin-tier or its
 *   subtle fallback ring, nobody can tell the UI moved under it
 * - if FeedbackPinLayer stops publishing __mdtPinPositions, agents lose the
 *   tier read-back the UI shows
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const LAYER = fileURLToPath(new URL('../../src/lib/components/rb/FeedbackPinLayer.svelte', import.meta.url));

const ENTRY = [
	"export { default as Markers } from '$lib/components/rb/feedback/FeedbackPinMarkers.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

function pin(overrides = {}) {
	return {
		id: 'p1',
		x_pct: 12.5,
		y_pct: 40,
		anchor: '#deck-a',
		page: '/performance',
		text: 'tagged pin',
		created_at: '2026-10-01T00:00:00Z',
		build: { git_sha: 'abc', built_at_utc: '2026-10-01T00:00:00Z', source: 'test' },
		status: 'open',
		attachment: null,
		...overrides
	};
}

let mod;

before(async () => {
	mod = await loadSvelteSsrModule(ENTRY);
});

function render(pins, positions) {
	const props = { pins, seen: {}, pathname: '/performance', onopen: () => {} };
	if (positions !== undefined) props.positions = positions;
	return mod.render(mod.Markers, { props }).body;
}

test('a pin with no resolved position draws at its stored percentages, unmarked', () => {
	const html = render([pin()]);
	assert.match(html, /left:12\.5%;top:40%/);
	assert.match(html, /data-pin-tier="viewport"/);
	assert.doesNotMatch(html, /fb-pos-fallback/);
});

test('a pin re-placed on a nearby anchor draws at its px point with a tier and fallback mark', () => {
	const positions = new Map([
		['p1', { tier: 'anchor', fallback: true, via: '[data-testid="mixer"]', x: 580, y: 130, x_pct: 58, y_pct: 16.25 }]
	]);
	const html = render([pin()], positions);
	assert.match(html, /left:580px;top:130px/);
	assert.match(html, /data-pin-tier="anchor"/);
	assert.match(html, /data-pin-id="p1"/);
	assert.match(html, /fb-pos-fallback/);
	assert.match(html, /placed via \[data-testid=&quot;mixer&quot;\]|placed via \[data-testid="mixer"\]/);
});

test('a pin on its own element is drawn there without the fallback mark', () => {
	const positions = new Map([['p1', { tier: 'element', fallback: false, via: '#deck-a', x: 200, y: 250, x_pct: 20, y_pct: 31.25 }]]);
	const html = render([pin()], positions);
	assert.match(html, /left:200px;top:250px/);
	assert.match(html, /data-pin-tier="element"/);
	assert.doesNotMatch(html, /fb-pos-fallback/);
});

test('FeedbackPinLayer publishes and removes window.__mdtPinPositions', () => {
	const src = readFileSync(LAYER, 'utf8');
	assert.match(src, /_globals\(\)\.__mdtPinPositions = \{/);
	assert.match(src, /delete _globals\(\)\.__mdtPinPositions;/);
	assert.match(src, /resolvePinPosition\(pin, measureSelector, viewport\)/);
	assert.match(src, /capturePinPlacement\(/);
});
