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
 *
 * BUTTON_GLYPHS are ordinary action symbols (undo/redo arrows, window
 * controls, close crosses, back and collapse arrows). They are also ordinary
 * text and punctuation (an U+2014 character in toast prose, "1280×800"), so they are
 * banned only where they are a CONTROL: inside a <button>'s content, not in
 * its attributes and not as a word joiner between two letters or digits
 * (Codex BLOCKING 4133648260, PR #3896).
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

// The U+2014 character is spelled as an escape so this file carries no literal one.
const BUTTON_GLYPHS = ['↶', '↷', '▢', '\u2014', '▸', '←', '‹', '›', '■', '▯', '┃', '┆', '✕', '×'];
const BUTTON_GLYPH_SET = new Set(BUTTON_GLYPHS);

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
	['mdash', '\u2014'],
	['lsaquo', '‹'],
	['rsaquo', '›'],

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

	['src/lib/components/rb/BrowserPanel.svelte', new Set(['▾', '←', '‹', '›'])], // button glyphs: 3356 ← Back, 3489 ‹ / › collapse
	['src/lib/components/rb/ToastStack.svelte', new Set(['▾', '▸'])], // button glyph: 164 ▸ expand
	['src/lib/components/rb/browser/AutoPlayWalkthrough.svelte', new Set(['😊', '🙃'])],
	['src/lib/components/rb/browser/RecentlyDeletedFolder.svelte', new Set(['↻'])],
	['src/lib/components/rb/deck/HotCueBank.svelte', new Set(['✎', '×'])], // button glyph: 326, 385 × clear
	['src/lib/components/rb/deck/LoopCluster.svelte', new Set(['▾', '▯'])], // button glyph: 280 ▯▯ mode
	['src/routes/admin/LyricSourceOrder.svelte', new Set(['▲', '▼'])],
	['src/routes/progress-tree/DepGraph.svelte', new Set(['⚠', '↗', '✕'])], // button glyph: 276 ✕ close
	['src/routes/progress-tree/NodeDetail.svelte', new Set(['↗'])],
	['src/routes/progress-tree/NodeRow.svelte', new Set(['▾'])],
	['src/routes/progress-tree/StatusChip.svelte', new Set(['✓'])],

	// Third group: BUTTON_GLYPHS already used as controls when the button-scoped
	// ban landed (Codex BLOCKING 4133648260). Only the two components Codex
	// named (LibrarySourceTabs undo/redo, MidiLearnLogPopout window controls)
	// were converted; the rest are tracked in .planning/debt/3896.md. Lines are
	// where each glyph sat at that commit, for finding them, not for matching.
	['src/lib/components/lyrics/StageOverlay.svelte', new Set(['×'])], // 282
	['src/lib/components/rb/CreatePairingSheet.svelte', new Set(['×'])], // 141, 162
	['src/lib/components/rb/Deck.svelte', new Set(['┃', '┆'])], // 452 ┃┃┃, 455 ┆┆┆
	['src/lib/components/rb/FeedbackPinCard.svelte', new Set(['×'])], // 277, 296
	['src/lib/components/rb/FeedbackSupportPanel.svelte', new Set(['×'])], // 31
	['src/lib/components/rb/MidiPanel.svelte', new Set(['×'])], // 119
	['src/lib/components/rb/browser/PlaylistTree.svelte', new Set(['×'])], // 346
	['src/lib/components/rb/browser/PreviewStrip.svelte', new Set(['■'])], // 296 stop
	['src/lib/components/rb/browser/SearchBox.svelte', new Set(['×'])], // 128
	['src/lib/components/rb/browser/SpotifySourcePanel.svelte', new Set(['×'])], // 72
	['src/lib/components/rb/browser/TrackTable.svelte', new Set(['×'])], // 1802
	['src/lib/components/rb/deck/LoopSafetyControls.svelte', new Set(['×'])], // 89
	['src/lib/components/smartlists/RuleGroup.svelte', new Set(['×'])], // 40
	['src/lib/components/smartlists/RulePredicate.svelte', new Set(['×'])], // 49, 63
	['src/routes/pairings/+page.svelte', new Set(['×'])], // 80
	['src/routes/track/[stable_id]/+page.svelte', new Set(['×'])] // 107
]);

/** Comments removed; their newlines kept, so a finding's line number is the file's. */
function stripComments(source) {
	const blank = (m) => m.replace(/[^\n]/g, '');
	return source
		.replace(/\/\*[\s\S]*?\*\//g, blank)
		.replace(/^[ \t]*\/\/.*$/gm, '')
		.replace(/<!--[\s\S]*?-->/g, blank);
}

/**
 * Index of the `>` that closes the tag opening at `start`. Brace- and
 * quote-aware, because Svelte attributes hold arrow functions
 * (`onclick={() => x}`) whose `>` must not end the tag.
 */
function tagEnd(source, start) {
	let depth = 0;
	let quote = null;
	for (let i = start + 1; i < source.length; i++) {
		const c = source[i];
		if (quote !== null) {
			if (c === quote && source[i - 1] !== '\\') quote = null;
			continue;
		}
		if (c === '"' || (depth > 0 && (c === "'" || c === '`'))) quote = c;
		else if (c === '{') depth++;
		else if (c === '}') depth--;
		else if (c === '>' && depth === 0) return i;
	}
	return -1;
}

/** A button body with every nested tag blanked (newlines kept), so only content remains. */
function blankTags(body) {
	let out = '';
	let i = 0;
	while (i < body.length) {
		if (body[i] === '<' && /[a-zA-Z/]/.test(body[i + 1] ?? '')) {
			const end = tagEnd(body, i);
			if (end === -1) break;
			out += body.slice(i, end + 1).replace(/[^\n]/g, ' ');
			i = end + 1;
		} else {
			out += body[i++];
		}
	}
	return out;
}

const WORD_CHAR = /[\p{L}\p{N}]/u;

/** The nearest non-blank character before or after index `i`, on the same line. */
function neighbor(text, i, step) {
	for (let j = i + step; j >= 0 && j < text.length; j += step) {
		if (text[j] === ' ' || text[j] === '\t') continue;
		return text[j] === '\n' ? '' : text[j];
	}
	return '';
}

/**
 * BUTTON_GLYPHS used as a control: inside a <button>'s content (text or a
 * string rendered there), and not a joiner between two words or numbers
 * ("1280×800", "Save \u2014 then close" stay text). Returns [{ glyph, line }].
 */
function buttonControlGlyphs(source) {
	const found = [];
	for (const open of source.matchAll(/<button\b/g)) {
		const openEnd = tagEnd(source, open.index);
		if (openEnd === -1) continue;
		const close = source.indexOf('</button', openEnd);
		if (close === -1) continue;
		const body = blankTags(source.slice(openEnd + 1, close));
		for (let i = 0; i < body.length; i++) {
			if (!BUTTON_GLYPH_SET.has(body[i])) continue;
			if (WORD_CHAR.test(neighbor(body, i, -1)) && WORD_CHAR.test(neighbor(body, i, 1))) continue;
			const line = source.slice(0, openEnd + 1 + i).split('\n').length;
			found.push({ glyph: body[i], line });
		}
	}
	return found;
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

		const controls = buttonControlGlyphs(source).filter((f) => !allowed.has(f.glyph));
		assert.deepEqual(
			controls,
			[],
			`${rel} uses text-symbol glyphs as button controls: ${controls.map((f) => `${f.glyph} at line ${f.line}`).join(', ')} -- draw an inline SVG icon`
		);
	});
}

test('CHROME-01: KNOWN_DEBT grandfathers exactly the glyphs each file contains, nothing wider', () => {
	for (const [rel, allowed] of KNOWN_DEBT) {
		assert.ok(scanned.includes(rel), `KNOWN_DEBT names ${rel}, which the sweep no longer finds -- remove the stale entry`);
		const source = scannable(rel);
		const controlGlyphs = new Set(buttonControlGlyphs(source).map((f) => f.glyph));
		for (const glyph of allowed) {
			if (BUTTON_GLYPH_SET.has(glyph)) {
				assert.ok(
					controlGlyphs.has(glyph),
					`KNOWN_DEBT for ${rel} lists ${glyph}, but no button there uses it as a control any more -- shrink the entry`
				);
				continue;
			}
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

test('CHROME-01: a text-symbol action glyph used as a button control is caught', () => {
	const glyphsOf = (src) => buttonControlGlyphs(decodeEncodedGlyphs(src)).map((f) => f.glyph);
	for (const glyph of BUTTON_GLYPHS) {
		assert.deepEqual(glyphsOf(`<button type="button">${glyph}</button>`), [glyph], `lone ${glyph}`);
	}
	assert.deepEqual(glyphsOf('<button onclick={() => (open = false)}>&times;</button>'), ['×'], 'entity, arrow fn attr');
	assert.deepEqual(glyphsOf("<button>{min ? '▢' : '\u2014'}</button>"), ['▢', '\u2014'], 'string literals rendered');
	assert.deepEqual(glyphsOf('<button>\n\t\u2190 Back\n</button>'), ['←'], 'glyph leading a label');
	assert.deepEqual(glyphsOf('<button>&times; group</button>'), ['×'], 'glyph leading a label, entity');
	assert.deepEqual(glyphsOf('<button><span class="remove">×</span><small>LO</small></button>'), ['×'], 'inside a nested span');
	assert.deepEqual(glyphsOf('<button>Back \u2190</button>'), ['←'], 'glyph trailing a label');
});

test('CHROME-01: the same characters as ordinary text or punctuation still pass', () => {
	const glyphsOf = (src) => buttonControlGlyphs(decodeEncodedGlyphs(src)).map((f) => f.glyph);
	assert.deepEqual(glyphsOf('<button>Resize to 1280×800</button>'), [], 'a dimension inside a label');
	assert.deepEqual(glyphsOf('<button>Save \u2014 then close</button>'), [], 'an U+2014 character between words');
	assert.deepEqual(glyphsOf('<p>\u2014</p><dd>{w}×{h}</dd><span>× 2</span>'), [], 'outside any button');
	assert.deepEqual(glyphsOf('<button title="a \u2014 b" aria-label="x × y">Go</button>'), [], 'in the button attributes');
	assert.deepEqual(glyphsOf("<button onclick={() => go('×')}>Go</button>"), [], 'a > inside an attribute does not end the tag');
	assert.deepEqual(glyphsOf('<button><span title="a \u2014 b">Go</span></button>'), [], 'in a nested tag attribute');

	// Real prose already on this branch: the toast copy's U+2014 characters and the
	// feedback card's viewport readout pass, and the scan did see them.
	const toast = scannable('src/lib/rb/reanalyze-batch-feedback.ts');
	assert.ok(toast.includes('\u2014'), 'control: the toast prose carries U+2014 characters');
	assert.deepEqual(buttonControlGlyphs(toast), []);
	const pin = scannable('src/lib/components/rb/FeedbackPinCard.svelte');
	const viewportLine = pin.slice(0, pin.indexOf('}×{')).split('\n').length;
	assert.ok(pin.includes('}×{'), 'control: the feedback card shows a viewport as W×H');
	const pinLines = buttonControlGlyphs(pin).map((f) => f.line);
	assert.ok(pinLines.length >= 2, 'control: the card close buttons are still seen');
	assert.equal(pinLines.includes(viewportLine), false, 'the viewport readout is text, not a control');
});

test('CHROME-01: the Codex-named controls draw SVG icons with an accessible name and a title', () => {
	const cases = [
		['src/lib/components/rb/browser/LibrarySourceTabs.svelte', ['data-testid="playlist-undo"', 'data-testid="playlist-redo"']],
		['src/lib/components/rb/midi/MidiLearnLogPopout.svelte', ['minimize MIDI log', 'aria-label="close MIDI log"']]
	];
	for (const [rel, markers] of cases) {
		assert.equal(KNOWN_DEBT.has(rel), false, `${rel} was converted, so it carries no debt entry`);
		const source = scannable(rel);
		assert.deepEqual(buttonControlGlyphs(source), [], rel);
		for (const marker of markers) {
			const at = source.indexOf(marker);
			assert.ok(at >= 0, `${rel}: ${marker}`);
			const start = source.lastIndexOf('<button', at);
			const button = source.slice(start, source.indexOf('</button', at));
			assert.match(button, /aria-label=/, `${rel}: ${marker} has an aria-label`);
			assert.match(button, /title=/, `${rel}: ${marker} has a title`);
			assert.match(button, /<svg[^>]*aria-hidden="true"/, `${rel}: ${marker} draws an SVG icon`);
		}
	}
});
