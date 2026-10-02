/**
 * House rule: numeric readouts (counts, Hz, ms, ratios, durations) carry a
 * hover `title` explaining what the number is. The /performance route is
 * guarded by performance-numeric-readout-explainers.test.mjs against its
 * audit inventory; this guard covers the library-side pages that have no
 * such inventory: /play-analytics, /sets and /dedup.
 *
 * How: a small markup scanner walks each page's template, keeps a stack of
 * open elements, and for every text interpolation `{expr}` whose expression
 * reads a numeric field (counts, lengths, durations, seconds, percentages,
 * ids, BPM, ratings, pids, timestamps) requires a `title` on the element
 * that holds it or on its direct parent.
 *
 * Regression lines:
 * - if a numeric readout on these pages loses (or never had) its title,
 *   the scan names the file, line and expression
 * - if the scanner stops recognizing readouts (returns zero) the presence
 *   floor per file fails, so a broken scanner cannot pass by finding nothing
 * - if the scanner misses a stripped title, the in-memory mutation test fails
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const ROUTES = {
	'play-analytics': 10,
	sets: 16,
	dedup: 9
};

const read = (route) =>
	readFileSync(fileURLToPath(new URL(`../../src/routes/${route}/+page.svelte`, import.meta.url)), 'utf8');

/** Expressions that render a number (or a number-derived string). */
const NUMERIC_EXPR =
	/(\.length\b|count\b|_count\b|duration|_s\b|_ms\b|\.plays\b|\.sessions\b|unique_tracks|similarity|\.bpm\b|\.rating\b|\.pid\b|toFixed|Math\.|confidence|cluster_id|formatDate|toLocaleDateString)/;

const VOID = new Set(['input', 'img', 'br', 'hr', 'meta', 'link', 'source', 'col', 'area', 'wbr']);

/** The template only: drop <script> and <style> blocks, keep line numbers. */
function templateOf(source) {
	return source.replace(/<(script|style)\b[\s\S]*?<\/\1>/g, (block) => block.replace(/[^\n]/g, ' '));
}

/** Read one `{...}` starting at i (balanced, string-aware); returns end index. */
function braceEnd(text, i) {
	let depth = 0;
	let quote = null;
	for (let j = i; j < text.length; j++) {
		const c = text[j];
		if (quote) {
			if (c === '\\') j++;
			else if (c === quote) quote = null;
			continue;
		}
		if (c === "'" || c === '"' || c === '`') quote = c;
		else if (c === '{') depth++;
		else if (c === '}' && --depth === 0) return j;
	}
	throw new Error(`unbalanced brace at ${i}`);
}

/** Read one tag starting at '<' (attribute braces may contain '>'). */
function tagEnd(text, i) {
	let quote = null;
	for (let j = i + 1; j < text.length; j++) {
		const c = text[j];
		if (quote) {
			if (c === quote) quote = null;
			continue;
		}
		if (c === '"' || c === "'") quote = c;
		else if (c === '{') j = braceEnd(text, j);
		else if (c === '>') return j;
	}
	throw new Error(`unterminated tag at ${i}`);
}

/** Every numeric text interpolation, with whether it is explained by a title. */
export function numericReadouts(source) {
	const text = templateOf(source);
	const stack = [];
	const readouts = [];
	let i = 0;
	while (i < text.length) {
		const c = text[i];
		if (c === '<' && /[a-zA-Z/]/.test(text[i + 1] ?? '')) {
			const end = tagEnd(text, i);
			const raw = text.slice(i, end + 1);
			const name = raw.match(/^<\/?([a-zA-Z][\w:-]*)/)[1].toLowerCase();
			if (raw.startsWith('</')) {
				const at = stack.map((e) => e.name).lastIndexOf(name);
				if (at !== -1) stack.length = at;
			} else if (!raw.endsWith('/>') && !VOID.has(name)) {
				stack.push({ name, titled: /\stitle=/.test(raw) });
			}
			i = end + 1;
			continue;
		}
		if (c === '{') {
			const end = braceEnd(text, i);
			const expr = text.slice(i + 1, end).trim();
			if (!/^[#/:@]/.test(expr) && NUMERIC_EXPR.test(expr)) {
				const holder = stack.at(-1);
				const parent = stack.at(-2);
				readouts.push({
					expr,
					line: text.slice(0, i).split('\n').length,
					titled: Boolean(holder?.titled || parent?.titled)
				});
			}
			i = end + 1;
			continue;
		}
		i++;
	}
	return readouts;
}

for (const [route, floor] of Object.entries(ROUTES)) {
	test(`routes/${route}: every numeric readout carries a hover title`, () => {
		const readouts = numericReadouts(read(route));
		assert.ok(
			readouts.length >= floor,
			`scanner found ${readouts.length} numeric readouts, expected at least ${floor}`
		);
		const untitled = readouts.filter((r) => !r.titled).map((r) => `line ${r.line}: {${r.expr}}`);
		assert.deepEqual(untitled, [], `numeric readouts without a title in routes/${route}`);
	});
}

test('mutation: stripping one title is caught and named', () => {
	const source = read('sets').replace(
		'<time title="Seconds from the start of the session to this event">',
		'<time>'
	);
	assert.notEqual(source, read('sets'), 'the mutation actually applied');
	const untitled = numericReadouts(source).filter((r) => !r.titled);
	assert.equal(untitled.length, 1);
	assert.match(untitled[0].expr, /timestamp_s\.toFixed/);
});

test('scanner control: a readout titled only on a grandparent does not count', () => {
	const sample = '<section title="x"><div><span>{items.length}</span></div></section>';
	assert.deepEqual(
		numericReadouts(sample).map((r) => r.titled),
		[false]
	);
	const ok = '<div title="count"><strong>{items.length}</strong></div>';
	assert.deepEqual(
		numericReadouts(ok).map((r) => r.titled),
		[true]
	);
});
