/**
 * Codex review (PR #5822, P1): `/performance` unmount starts the plan runner's
 * asynchronous disposal, and the runner may still cancel an armed launch on the
 * engine. Engine teardown must therefore wait for the mirror to close, not run
 * beside it. The page wiring has no mount harness, so this asserts the ordering
 * in its source: the chained teardown is present, and the bare call is not.
 *
 * [if] unmount runs the engine teardown synchronously [then ⛔] the armed launch
 *   can be cancelled against a disposed graph.
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

const source = await readFile(new URL('../../src/routes/performance/+page.svelte', import.meta.url), 'utf8');

test('performance unmount tears the engine down only after the mirror has closed', () => {
	assert.match(source, /if \(disposeOnUnmount\)\s+void mirrorClosed\.finally\(\(\) => \{\s+if \(currentPerformanceMount\(\) === mountGeneration\) void beginTeardown\(\);\s+\}\);/);
	assert.doesNotMatch(source, /if \(disposeOnUnmount\) void beginTeardown\(\);/);
});

test('a remounted page is not torn down by the retiring page: the teardown checks the mount claim', () => {
	assert.doesNotMatch(source, /mirrorClosed\.finally\(\(\) => beginTeardown\(\)\)/);
	assert.match(source, /const mountGeneration = claimPerformanceMount\(\);/);
});

test('the mount claim is taken synchronously in onMount, before the hydration await', () => {
	// Codex P1 (PR #5822, 218714d893): an epoch bumped only by the IPC install lags a remount whose
	// hydration is still pending, so the retiring page would still see itself as the owner.
	const claim = source.indexOf('const mountGeneration = claimPerformanceMount();');
	const hydrate = source.indexOf('void hydratePerformanceFeedback()');
	assert.ok(claim > 0, 'the mount claim is missing');
	assert.ok(hydrate > claim, 'the mount claim must precede the hydration await');
});
