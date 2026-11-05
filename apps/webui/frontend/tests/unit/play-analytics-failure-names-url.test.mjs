/**
 * Play-analytics e2e specs must name leftover failed-resource URLs in assertion
 * diffs, and must await the update-check response before the final console scan.
 *
 * Acceptance tests:
 *
 * - [if] either play-analytics spec still uses failedResources.every(
 *   [then ⛔️] this test fails with the offending substring.
 * - [if] the populated spec lacks the filter form on failedResources
 *   [then ⛔️] this test fails before a bare empty scan could pass.
 * - [if] await updateCheck sits after the final consoleErrors assertion
 *   [then ⛔️] this test fails with index evidence.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const FRONTEND_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const POPULATED_SPEC = join(FRONTEND_ROOT, 'tests', 'e2e', 'play-analytics.spec.ts');
const EMPTY_SPEC = join(FRONTEND_ROOT, 'tests', 'e2e', 'play-analytics-empty.spec.ts');

const FILTER_ASSERTION =
	"expect(failedResources.filter(({ url }) => !url.endsWith('/favicon.svg'))).toEqual([]);";

test('play-analytics specs must not use failedResources.every(', () => {
	for (const [label, path] of [
		['play-analytics.spec.ts', POPULATED_SPEC],
		['play-analytics-empty.spec.ts', EMPTY_SPEC]
	]) {
		const source = readFileSync(path, 'utf-8');
		assert.ok(
			!source.includes('failedResources.every('),
			`${label} must not use failedResources.every(` +
				`; use failedResources.filter(...).toEqual([]) so failures name URLs`
		);
	}
});

test('play-analytics.spec.ts pins the filter form on failedResources', () => {
	const source = readFileSync(POPULATED_SPEC, 'utf-8');
	assert.ok(
		source.includes(FILTER_ASSERTION),
		'play-analytics.spec.ts must assert failedResources.filter(...).toEqual([])'
	);
});

test('play-analytics.spec.ts awaits updateCheck before the final consoleErrors assertion', () => {
	const source = readFileSync(POPULATED_SPEC, 'utf-8');
	const updateCheckIndex = source.indexOf('await updateCheck');
	assert.ok(updateCheckIndex >= 0, 'play-analytics.spec.ts must await updateCheck');
	const consoleTail = "expect(consoleErrors.filter((error) => !error.includes('favicon'))).toEqual([]);";
	const consoleIndex = source.lastIndexOf(consoleTail);
	assert.ok(consoleIndex >= 0, 'play-analytics.spec.ts must end with the favicon-filtered consoleErrors assertion');
	assert.ok(
		updateCheckIndex < consoleIndex,
		'await updateCheck must appear before the final consoleErrors assertion'
	);
});
