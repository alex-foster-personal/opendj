import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

/**
 * The engine's SOURCE-TEXT surface, as a list of files rather than one path.
 *
 * Two drift guards (control-explainer-phase-lock, load-memory-kpis) read the
 * engine as text instead of through its exports, because what they pin is
 * arithmetic that is duplicated in the UI copy - a value the module never
 * returns to a caller. That is legitimate, but it hardcoded
 * `src/lib/rb/audio-engine.svelte.ts`.
 *
 * T4 splits that file into `src/lib/player/*` behind a re-export barrel. A
 * guard pointed at the barrel greps 90 lines of `export ... from` and finds
 * nothing, so it either fails for the wrong reason or - the real hazard - slices
 * an empty body out of a -1 index and asserts nothing at all while still
 * reporting green.
 *
 * So: the guards read this LIST, and every helper below refuses to return a
 * body it did not positively locate. Each extraction step appends the new module
 * path in the SAME commit that moves the symbol.
 *
 * Regression lines:
 * - if a listed path stops existing or reads empty then the guard is pointed at
 *   nothing and must fail rather than assert against ''
 * - if an anchor matches zero times then the symbol moved and the guard must be
 *   re-pointed, not deleted
 * - if an anchor matches more than once then the slice is ambiguous and the
 *   guard could pin the wrong body
 */

const SRC = fileURLToPath(new URL('../../src', import.meta.url));

/**
 * Every file that together holds what used to be audio-engine.svelte.ts.
 * Seeded with today's single path; T4 appends `lib/player/*` entries.
 */
export const ENGINE_SOURCE_PATHS = ['lib/rb/audio-engine.svelte.ts'];

//-----------------------------------------------------------------------------
// reading
//-----------------------------------------------------------------------------

function _readEngineSources() {
	return ENGINE_SOURCE_PATHS.map((path) => {
		const text = readFileSync(`${SRC}/${path}`, 'utf8');
		assert.ok(
			text.trim().length > 0,
			`if ${path} reads empty then every source-text guard below silently asserts nothing`
		);
		return { path, text };
	});
}

function _countOccurrences(text, needle) {
	let count = 0;
	for (let at = text.indexOf(needle); at !== -1; at = text.indexOf(needle, at + 1)) count += 1;
	return count;
}

//-----------------------------------------------------------------------------
// anchored extraction
//-----------------------------------------------------------------------------

/**
 * The body of the block opened by `anchor`, brace-matched to its close.
 *
 * `anchor` MUST be the declaration text up to and including the block's opening
 * brace, so that a reshaped signature re-points the guard loudly instead of
 * matching a return-type object literal by accident. It must occur exactly once
 * across the whole engine surface.
 */
export function engineBlockAfter(anchor) {
	assert.ok(
		anchor.endsWith('{'),
		`engine anchors must end at the block's opening brace, got: ${anchor}`
	);

	const sources = _readEngineSources();
	const total = sources.reduce((sum, file) => sum + _countOccurrences(file.text, anchor), 0);
	assert.equal(
		total,
		1,
		`if "${anchor}" does not occur exactly once across ` +
			`[${ENGINE_SOURCE_PATHS.join(', ')}] (found ${total}) then this drift guard is ` +
			'pointed at nothing and must be re-pointed, not deleted'
	);

	const file = sources.find((entry) => entry.text.includes(anchor));
	const { text } = file;
	const open = text.indexOf(anchor) + anchor.length - 1;

	let depth = 0;
	for (let i = open; i < text.length; i += 1) {
		if (text[i] === '{') depth += 1;
		else if (text[i] === '}') {
			depth -= 1;
			if (depth === 0) {
				const body = text.slice(open + 1, i);
				assert.ok(
					body.trim().length > 0,
					`if the block after "${anchor}" is empty then the guard asserts nothing`
				);
				return body;
			}
		}
	}
	throw new Error(`unbalanced braces after engine anchor in ${file.path}: ${anchor}`);
}
