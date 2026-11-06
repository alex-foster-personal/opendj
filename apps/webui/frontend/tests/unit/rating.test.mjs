/**
 * Issue #2572: every UI rating read goes through normalizeRating.
 *
 * No UI consumer may compare or display a raw rating without normalizeRating.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const { normalizeRating } = await loadTypeScriptModule('src/lib/rating.ts');

function read(relative) {
	return readFileSync(fileURLToPath(new URL(`../../${relative}`, import.meta.url)), 'utf8');
}

const CASES = [
	[0, 0],
	[51, 1],
	[102, 2],
	[153, 3],
	[204, 4],
	[255, 5],
	[1, 1],
	[2, 2],
	[3, 3],
	[4, 4],
	[5, 5],
	[null, null],
	[undefined, null]
];

test('normalizeRating maps POPM steps and passes canonical 0-5', () => {
	for (const [input, expected] of CASES) {
		assert.equal(normalizeRating(input), expected);
	}
});

test('normalizeRating rejects non-step and non-finite values', () => {
	for (const bad of [6, 50, 256, -1, 1.5, Number.NaN, Number.POSITIVE_INFINITY]) {
		assert.equal(normalizeRating(bad), null);
	}
});

test('normalizeRating is idempotent on every pinned case', () => {
	for (const [input] of CASES) {
		const once = normalizeRating(input);
		assert.equal(normalizeRating(once), once);
	}
	for (const bad of [6, 50, 256, -1, 1.5, Number.NaN]) {
		assert.equal(normalizeRating(normalizeRating(bad)), null);
	}
});

test('key UI sources import normalizeRating from $lib/rating', () => {
	const mustImport = [
		'src/lib/components/rb/browser/RatingStars.svelte',
		'src/lib/components/library/TrackInspectorView.svelte',
		'src/lib/components/rb/browser/browser-row-wire.ts',
		'src/lib/rb/track-info.ts',
		'src/lib/api.ts',
		'src/routes/track/[stable_id]/+page.svelte'
	];
	for (const path of mustImport) {
		const src = read(path);
		const importPattern =
			path === 'src/lib/api.ts'
				? /import\s*\{\s*normalizeRating\s*\}\s*from\s*['"](\$lib\/rating|\.\/rating)['"]/
				: /normalizeRating[\s\S]*from ['"]\$lib\/rating['"]|from ['"]\$lib\/rating['"][\s\S]*normalizeRating/;
		assert.match(src, importPattern, `${path} must import normalizeRating`);
	}
	assert.match(read('src/lib/rating.ts'), /export function normalizeRating/);
});
