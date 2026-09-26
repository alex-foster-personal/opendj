/**
 * requirement: CHROME-01
 * UI chrome uses SVG icons, not Unicode emoji or text-symbol glyphs.
 *
 * This sweeps every non-test .svelte/.ts file under src/lib and src/routes
 * rather than checking a fixed file list. The issue's own note is explicit:
 * "Enforce it with a test, not a sweep that rots" -- a fixed list stops
 * catching anything the moment a new component is added, which is exactly
 * the failure mode a hardcoded list produces. the maintainer restated the rule as
 * global ("Icon not Emoji plz - general rule throughout for UI. (and global
 * tbh)"), so the scan is repo-wide.
 *
 * KNOWN_DEBT is a closed, named, glyph-specific exception list for
 * violations that pre-date this rule and sit outside this PR's seven pins
 * (deck transport, browser tree-fold chevrons, an admin table's sort
 * indicators, and progress-tree status glyphs). It grandfathers exactly the
 * glyphs recorded against each file: any OTHER glyph added to one of those
 * files still fails the sweep, and it does not shrink the search space for
 * any file not named here. Fixing one of these files means removing its
 * entry, not widening it.
 */
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join, relative } from 'node:path';
import { test } from 'node:test';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const SRC_ROOTS = ['src/lib', 'src/routes'];

const BANNED_GLYPHS = ['✓', '✗', '⚡', '★', '☆', '▲', '▼', '↻', '✅', '🤖'];

/**
 * Unicode Extended_Pictographic covers emoji blocks used as UI glyphs.
 * Two separate regex objects, deliberately: a stateful `g`-flagged one for
 * `matchAll` (which needs `g`) and a stateless one for `.test()` calls
 * elsewhere in this file -- sharing one `g`-flagged object across `.test()`
 * calls would let its `lastIndex` leak between assertions and silently skip
 * matches on some inputs.
 */
const EMOJI_CODEPOINT_RE_GLOBAL = /\p{Extended_Pictographic}/gu;
const EMOJI_CODEPOINT_RE = /\p{Extended_Pictographic}/u;

// Pre-existing, out-of-scope text-glyph usage. Each entry lists exactly the
// glyphs already present in that file, not "everything this file might ever
// contain" -- a new glyph in one of these files still fails.
const KNOWN_DEBT = new Map([
	['src/lib/components/rb/browser/TreeCurrentFold.svelte', new Set(['▲', '▼'])],
	['src/lib/components/rb/deck/DeckHeader.svelte', new Set(['⏏'])],
	['src/lib/components/rb/deck/StripWaveform.svelte', new Set(['▶'])],
	['src/lib/components/rb/wave/WaveRow.svelte', new Set(['⏏'])],
	['src/lib/rb/column-tips.ts', new Set(['▶'])],
	['src/routes/admin/format.ts', new Set(['▲', '▼'])],
	['src/routes/progress-tree/types.ts', new Set(['⏸', '✋', '⚠'])]
]);

function stripComments(source) {
	return source
		.replace(/\/\*[\s\S]*?\*\//g, '')
		.replace(/^\s*\/\/.*$/gm, '')
		.replace(/<!--[\s\S]*?-->/g, '');
}

function findEmojiCodepoints(source) {
	const found = new Set();
	for (const match of source.matchAll(EMOJI_CODEPOINT_RE_GLOBAL)) {
		found.add(match[0]);
	}
	return [...found];
}

function walkSourceFiles(absoluteDir) {
	const out = [];
	for (const entry of readdirSync(absoluteDir, { withFileTypes: true })) {
		if (entry.name === 'node_modules') continue;
		const full = join(absoluteDir, entry.name);
		if (entry.isDirectory()) {
			out.push(...walkSourceFiles(full));
		} else if (/\.(svelte|ts)$/.test(entry.name) && !/\.test\.(mjs|ts)$/.test(entry.name)) {
			out.push(full);
		}
	}
	return out;
}

const scanned = SRC_ROOTS.flatMap((root) => walkSourceFiles(join(FRONTEND_ROOT, root)))
	.map((absolute) => relative(FRONTEND_ROOT, absolute))
	.sort();

assert.ok(scanned.length > 500, `expected the repo-wide sweep to find hundreds of files, found ${scanned.length}`);

for (const rel of scanned) {
	test(`CHROME-01: ${rel} has no emoji or text-symbol UI glyphs`, () => {
		const source = stripComments(readFileSync(join(FRONTEND_ROOT, rel), 'utf8'));
		const allowed = KNOWN_DEBT.get(rel) ?? new Set();

		for (const glyph of BANNED_GLYPHS) {
			if (allowed.has(glyph)) continue;
			assert.equal(source.includes(glyph), false, `${rel} still contains banned glyph ${glyph}`);
		}

		const emoji = findEmojiCodepoints(source).filter((g) => !allowed.has(g));
		assert.equal(
			emoji.length,
			0,
			`${rel} contains emoji codepoints: ${emoji.map((g) => `${g} (U+${g.codePointAt(0).toString(16)})`).join(', ')}`
		);
	});
}

test('CHROME-01: KNOWN_DEBT grandfathers exactly the glyphs each file contains, nothing wider', () => {
	for (const [rel, allowed] of KNOWN_DEBT) {
		assert.ok(scanned.includes(rel), `KNOWN_DEBT names ${rel}, which the sweep no longer finds -- remove the stale entry`);
		const source = stripComments(readFileSync(join(FRONTEND_ROOT, rel), 'utf8'));
		for (const glyph of allowed) {
			const isBanned = BANNED_GLYPHS.includes(glyph);
			const isEmoji = EMOJI_CODEPOINT_RE.test(glyph);
			assert.ok(isBanned || isEmoji, `KNOWN_DEBT for ${rel} lists ${glyph}, which the sweep would not have flagged anyway`);
			assert.ok(source.includes(glyph), `KNOWN_DEBT for ${rel} lists ${glyph}, but the file no longer contains it -- shrink the entry`);
		}
	}
});

test('CHROME-01: emoji detector catches arbitrary pictographic codepoints', () => {
	assert.equal(EMOJI_CODEPOINT_RE.test('🎵'), true);
	assert.equal(EMOJI_CODEPOINT_RE.test('plain text'), false);
});
