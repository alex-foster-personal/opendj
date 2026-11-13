// requirement: CMDK-9600
/**
 * Palette exclusion reasons stay actionable (issue #13195).
 *
 * (private path), "Implementation": a menu id left out
 * of the palette carries a real reason that tells the user where the omitted action
 * lives instead (a right-click menu, a dialog, the bar's own rows, and so on).
 *
 * Regression lines:
 * - if a menu id is excluded from the palette then its reason names a surface the user can reach
 * - if an exclusion has an empty reason then broken
 * - if an exclusion has an implementation detail but no reachable surface then broken
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let palette;

before(async () => {
	palette = await loadTypeScriptModule('src/lib/palette/commands.ts');
});

/** Places in the app a user can go to perform an action the palette omits. */
const REACHABLE_SURFACE =
	/\b(menus?|bar|dialog|playlist tree|lane|waveform|buttons?|palette entry)\b/i;

/** Source-level wording a user cannot act on (file paths, source extensions). */
const SOURCE_DETAIL = /(\bsrc\/|\$lib\/|\.(ts|svelte|mjs|py)\b)/;

const MIN_REASON_LENGTH = 20;

/** Return why an exclusion reason is not actionable, or null when it is. */
function reasonProblem(reason) {
	if (typeof reason !== 'string' || reason.trim() === '') return 'empty reason';
	if (reason.trim().length < MIN_REASON_LENGTH) return 'reason too short to explain anything';
	if (SOURCE_DETAIL.test(reason)) return 'reason cites source code, not a user-facing surface';
	if (!REACHABLE_SURFACE.test(reason)) return 'reason names no surface the user can reach';
	return null;
}

/** Map every exclusion id whose reason is not actionable to its problem. */
function exclusionProblems(exclusions) {
	const problems = {};
	for (const [id, reason] of Object.entries(exclusions)) {
		const problem = reasonProblem(reason);
		if (problem !== null) problems[id] = problem;
	}
	return problems;
}

test('every exclusion reason tells the user where the omitted action lives', () => {
	// Each shipped reason must be non-empty, user-facing and name a reachable surface.
	assert.deepEqual(exclusionProblems(palette.PALETTE_REGISTRY.exclusions), {});
});

test('an empty or whitespace exclusion reason is rejected', () => {
	// Blanking a shipped reason must be caught, not counted as covered.
	const blanked = { ...palette.PALETTE_REGISTRY.exclusions, 'deck.load': '' };
	assert.deepEqual(exclusionProblems(blanked), { 'deck.load': 'empty reason' });
	const spaces = { ...palette.PALETTE_REGISTRY.exclusions, 'deck.load': '   \t ' };
	assert.deepEqual(exclusionProblems(spaces), { 'deck.load': 'empty reason' });
	assert.equal(reasonProblem(undefined), 'empty reason');
});

test('an implementation detail with no reachable surface is rejected', () => {
	// A reason that only explains the code path leaves the user nowhere to go.
	const detailOnly = {
		...palette.PALETTE_REGISTRY.exclusions,
		'deck.load': 'Handled by the PerformanceCommand load verb over IPC; needs a deck argument.'
	};
	assert.deepEqual(exclusionProblems(detailOnly), {
		'deck.load': 'reason names no surface the user can reach'
	});
	const sourcePath = {
		...palette.PALETTE_REGISTRY.exclusions,
		'deck.load': 'See src/lib/context-menu/registry.ts for the deck right-click menu row.'
	};
	assert.deepEqual(exclusionProblems(sourcePath), {
		'deck.load': 'reason cites source code, not a user-facing surface'
	});
	assert.equal(reasonProblem('Not wired.'), 'reason too short to explain anything');
});
