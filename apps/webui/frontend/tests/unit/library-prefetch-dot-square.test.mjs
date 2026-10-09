/**
 * LIBM-177 (the maintainer, Tue 6 Oct 2026): "orange dots on feature grid, should
 * presumably be square". The row's prefetching-audio dot sits on the
 * AnalysisDots grid, so it takes a grid cell's size and corner.
 *
 * @pytest.mark.requirement LIBM-177
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const TABLE = readFileSync('src/lib/components/rb/browser/TrackTable.svelte', 'utf8');
const DOTS = readFileSync('src/lib/components/rb/browser/AnalysisDots.svelte', 'utf8');

function ruleBody(src, selector) {
	const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
	const bodies = [...src.matchAll(new RegExp(`\\n\\s*${escaped} \\{([^}]*)\\}`, 'g'))].map((m) => m[1]);
	assert.equal(bodies.length, 1, `expected exactly one \`${selector} {}\` rule`);
	return bodies[0];
}
const prop = (body, name) => body.match(new RegExp(`(?:^|\\n)\\s*${name}:\\s*([^;]+);`))?.[1].trim();

test('if the prefetching-audio dot is round or a different size than a grid cell then it reads as foreign to the feature grid', () => {
	const dot = ruleBody(TABLE, '.audio-cache-dot');
	const cell = ruleBody(DOTS, '.dot');
	// Control: the grid cell itself is a small square the probe can read.
	assert.equal(prop(cell, 'width'), prop(cell, 'height'));
	assert.match(prop(cell, 'width') ?? '', /^\d+px$/);
	assert.equal(prop(dot, 'width'), prop(cell, 'width'));
	assert.equal(prop(dot, 'height'), prop(cell, 'height'));
	assert.equal(prop(dot, 'border-radius'), prop(cell, 'border-radius'));
	assert.doesNotMatch(dot, /border-radius:\s*50%/);
	// The marker stays the orange prefetch marker (overshoot control).
	assert.match(dot, /background:\s*#e89a3c/);
	assert.match(TABLE, /class="audio-cache-dot"\s+title="Prefetching audio"/);
});
