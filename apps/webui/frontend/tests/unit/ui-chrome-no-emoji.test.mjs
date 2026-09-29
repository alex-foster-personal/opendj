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

// ✎ is banned by name, not left to the emoji check: whether U+270E is
// Extended_Pictographic depends on the runtime's Unicode data (Node 22.14,
// Unicode 16: yes; Node 22.22, Unicode 17: no), so the verdict must not.
const BANNED_GLYPHS = ['✓', '✗', '⚡', '★', '☆', '▲', '▼', '▾', '↻', '✎', '✅', '🤖'];

/**
 * Musical accidentals are TEXT in key names ("F♯m", "B♭"), not icons, and
 * the runtime's Unicode data disagrees about them just as it does about ✎
 * (U+266D/U+266F are Extended_Pictographic under Unicode 16, not under 17).
 * They are never reported as emoji, on any runtime.
 */
const TEXT_SYMBOLS = new Set(['♭', '♮', '♯']);

/**
 * A glyph can reach the page spelled as an HTML entity (`&#9662;`,
 * `&#x25BE;`, `&blacktriangledown;`) or a JS escape (`\u25BE`, `\u{1F916}`),
 * and a search for the literal character never sees those (Codex P2
 * 4131234974: the pad-mode caret was `&#9662;`). Every file is decoded before
 * both checks below.
 *
 * Named entities are a closed list: a name the sweep does not know throws
 * rather than passing unread, so a new spelling of a banned glyph cannot slip
 * through a gap in this map. The first group is the benign names the tree
 * uses; the second spells the banned set (HTML has no name for ▲, ▼, ⚡, ✅
 * or 🤖).
 */
const NAMED_ENTITIES = new Map([
	['amp', '&'],
	['lt', '<'],
	['gt', '>'],
	['quot', '"'],
	['apos', "'"],
	['nbsp', '\u00a0'],
	['times', '×'],
	['larr', '←'],
	['rarr', '→'],
	['hellip', '…'],
	['middot', '·'],

	['check', '✓'],
	['checkmark', '✓'],
	['cross', '✗'],
	['starf', '★'],
	['bigstar', '★'],
	['star', '☆'],
	['dtrif', '▾'],
	['blacktriangledown', '▾'],
	['orarr', '↻'],
	['circlearrowright', '↻']
]);

const ENCODED_RE = /&#(\d+);|&#[xX]([0-9a-fA-F]+);|&([a-zA-Z][a-zA-Z0-9]*);|\\u\{([0-9a-fA-F]+)\}|\\u([0-9a-fA-F]{4})/g;

/** One pass, so `&amp;#9662;` (text that shows the entity) stays text. */
function decodeEncodedGlyphs(source) {
	return source.replace(ENCODED_RE, (_m, dec, hex, name, braced, u4) => {
		if (dec !== undefined) return String.fromCodePoint(Number(dec));
		const code = hex ?? braced ?? u4;
		if (code !== undefined) return String.fromCodePoint(parseInt(code, 16));
		const ch = NAMED_ENTITIES.get(name);
		if (ch === undefined) {
			throw new Error(`unknown named entity &${name}; -- add what it decodes to in NAMED_ENTITIES`);
		}
		return ch;
	});
}

/** What both checks read: comments stripped, every encoded glyph decoded. */
function scannable(rel) {
	return decodeEncodedGlyphs(stripComments(readFileSync(join(FRONTEND_ROOT, rel), 'utf8')));
}

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
//
// The second group was already on main (c890d5d68) when the sweep learned to
// decode entities and escapes and to ban ▾ (Codex P2 4131234974): each glyph
// there was either spelled encoded, so the literal search never saw it, or is
// a literal ▾, which the list did not name. Grandfathered on the same terms.
const KNOWN_DEBT = new Map([
	['src/lib/components/rb/browser/TreeCurrentFold.svelte', new Set(['▲', '▼'])],
	['src/lib/components/rb/deck/DeckHeader.svelte', new Set(['⏏'])],
	['src/lib/components/rb/deck/StripWaveform.svelte', new Set(['▶'])],
	['src/lib/components/rb/wave/WaveRow.svelte', new Set(['⏏'])],
	['src/lib/rb/column-tips.ts', new Set(['▶'])],
	['src/routes/admin/format.ts', new Set(['▲', '▼'])],
	['src/routes/progress-tree/types.ts', new Set(['⏸', '✋', '⚠'])],

	['src/lib/components/rb/BrowserPanel.svelte', new Set(['▾'])],
	['src/lib/components/rb/ToastStack.svelte', new Set(['▾'])],
	['src/lib/components/rb/browser/AutoPlayWalkthrough.svelte', new Set(['😊', '🙃'])],
	['src/lib/components/rb/browser/RecentlyDeletedFolder.svelte', new Set(['↻'])],
	['src/lib/components/rb/deck/HotCueBank.svelte', new Set(['✎'])],
	['src/lib/components/rb/deck/LoopCluster.svelte', new Set(['▾'])],
	['src/routes/admin/LyricSourceOrder.svelte', new Set(['▲', '▼'])],
	['src/routes/progress-tree/DepGraph.svelte', new Set(['⚠', '↗'])],
	['src/routes/progress-tree/NodeDetail.svelte', new Set(['↗'])],
	['src/routes/progress-tree/NodeRow.svelte', new Set(['▾'])],
	['src/routes/progress-tree/StatusChip.svelte', new Set(['✓'])]
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
		if (!TEXT_SYMBOLS.has(match[0])) found.add(match[0]);
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
		const source = scannable(rel);
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
		const source = scannable(rel);
		for (const glyph of allowed) {
			const isBanned = BANNED_GLYPHS.includes(glyph);
			const isEmoji = EMOJI_CODEPOINT_RE.test(glyph);
			assert.ok(isBanned || isEmoji, `KNOWN_DEBT for ${rel} lists ${glyph}, which the sweep would not have flagged anyway`);
			assert.ok(source.includes(glyph), `KNOWN_DEBT for ${rel} lists ${glyph}, but the file no longer contains it -- shrink the entry`);
		}
	}
});

test('CHROME-01: the verdict on ✎ and on accidentals does not depend on the runtime Unicode data', () => {
	// Real decoded sources: HotCueBank spells the pencil `&#9998;`, camelot.ts
	// spells the accidentals `\u266f` / `\u266d` (both already on main).
	assert.ok(scannable('src/lib/components/rb/deck/HotCueBank.svelte').includes('✎'));
	assert.ok(BANNED_GLYPHS.includes('✎'), 'the pencil must be banned by name, whatever the runtime says');
	const camelot = scannable('src/lib/player/key/camelot.ts');
	assert.ok(camelot.includes('♯') && camelot.includes('♭'), 'control: the decoder must see the accidentals');
	assert.deepEqual(findEmojiCodepoints(camelot), [], 'accidentals are key-name text, never emoji');
	assert.deepEqual(findEmojiCodepoints('🎵 F♯m'), ['🎵'], 'control: a real emoji beside them still reports');
});

test('CHROME-01: emoji detector catches arbitrary pictographic codepoints', () => {
	assert.equal(EMOJI_CODEPOINT_RE.test('🎵'), true);
	assert.equal(EMOJI_CODEPOINT_RE.test('plain text'), false);
});

test('CHROME-01: an encoded banned glyph is caught in every spelling', () => {
	for (const spelling of ['&#9662;', '&#x25BE;', '&#X25be;', '&dtrif;', '&blacktriangledown;', '\\u25BE', '\\u{25BE}']) {
		assert.equal(decodeEncodedGlyphs(`HOT CUE <span>${spelling}</span>`).includes('▾'), true, spelling);
	}
	assert.equal(EMOJI_CODEPOINT_RE.test(decodeEncodedGlyphs('&#x1F916;')), true, 'an entity-spelled emoji');
	assert.equal(EMOJI_CODEPOINT_RE.test(decodeEncodedGlyphs("'\\u{1F916}'")), true, 'an escaped emoji');
	assert.equal(EMOJI_CODEPOINT_RE.test(decodeEncodedGlyphs("'\\uD83E\\uDD16'")), true, 'a surrogate-pair escape');
	for (const [name, glyph] of NAMED_ENTITIES) {
		if (BANNED_GLYPHS.includes(glyph)) assert.equal(decodeEncodedGlyphs(`&${name};`), glyph, name);
	}
});

test('CHROME-01: legitimate entities decode to text the sweep allows', () => {
	const text = decodeEncodedGlyphs('a &amp; b&nbsp;c &lt;tag&gt; &times; &hellip; &#215;');
	assert.equal(text, 'a & b\u00a0c <tag> × … ×');
	for (const glyph of BANNED_GLYPHS) assert.equal(text.includes(glyph), false, glyph);
	assert.equal(EMOJI_CODEPOINT_RE.test(text), false);
	// Text that shows an entity is not the glyph it names.
	assert.equal(decodeEncodedGlyphs('&amp;#9662;'), '&#9662;');
});

test('CHROME-01: a named entity the sweep cannot decode fails loudly, never passes unread', () => {
	assert.throws(() => decodeEncodedGlyphs('&utrif;'), /unknown named entity &utrif;/);
});
