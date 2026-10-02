/**
 * Issue #3886 criterion 1 (V1 scope): "Icon not Emoji plz - general rule
 * throughout for UI". Status and action glyphs render as inline SVG icons,
 * never as emoji or text-symbol characters.
 *
 * Two sweeps, both over SOURCE with comments stripped and HTML entities /
 * JS escapes decoded (so `&#x2705;` or `\u{1F916}` cannot slip past a
 * literal search):
 *
 * 1. TOP BAR (strict): TopBar.svelte, every component it imports
 *    transitively under src/lib/components, and the glyph-producing
 *    midi-format.ts. Fails on ANY Extended_Pictographic codepoint and on the
 *    text-symbol glyphs commonly used as stand-in icons (check marks,
 *    crosses, stars, triangles), which are not all Extended_Pictographic.
 * 2. GLOBAL: every .svelte file under src/lib/components and src/routes.
 *    Fails on Extended_Pictographic codepoints except a closed, per-file,
 *    per-glyph KNOWN_DEBT list of pre-existing offenders outside the top
 *    bar. A NEW glyph, in a listed file or any other, fails.
 *
 * Regression lines:
 * - if an emoji or a check/cross glyph returns to the top bar then criterion
 *   1 is broken there again
 * - if a new emoji lands anywhere in UI markup then the global rule erodes
 * - if the scanner cannot see an emoji at all (comment stripping or decoding
 *   eating real markup) then both sweeps pass vacuously: the positive
 *   controls below prove each detector fires on a planted offender
 */
import assert from 'node:assert/strict';
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const FRONTEND = fileURLToPath(new URL('../..', import.meta.url));
const LIB = join(FRONTEND, 'src/lib');
const EMOJI_RE = /\p{Extended_Pictographic}/gu;

/** Musical accidentals are text in key names ("F♯m"); whether they are
 * Extended_Pictographic depends on the runtime's Unicode data. */
const TEXT_SYMBOLS = new Set(['♭', '♮', '♯']);

/** Text-symbol characters used as stand-in icons. Not all of them are
 * Extended_Pictographic (U+2713 and U+2717 are not), so the top bar bans
 * them by name. */
const TOPBAR_BANNED_GLYPHS = [
	'✓', '✔', '✗', '✘', '✅', '❌', '★', '☆', '⚡', '▶', '▲', '▼', '▾', '▸', '↻', '⟳', '✎', '⏏', '▢', '⚠'
];

/** Pre-existing Extended_Pictographic glyphs outside the top bar, measured
 * Fri 2 Oct 2026 on f88353f5. Grandfathered per file AND per glyph: fixing
 * one means deleting its entry, never widening it. */
const KNOWN_DEBT = new Map([
	['src/lib/components/TrackRow.svelte', ['★', '☆']],
	['src/lib/components/rb/browser/LibrarySourceTabs.svelte', ['☰']],
	['src/lib/components/rb/browser/PlaylistTree.svelte', ['✎']],
	['src/lib/components/rb/browser/RatingStars.svelte', ['★', '☆']],
	['src/lib/components/rb/browser/TrackTable.svelte', ['▶', '⚡']],
	['src/lib/components/rb/deck/DeckHeader.svelte', ['⏏']],
	['src/lib/components/rb/deck/StripWaveform.svelte', ['▶']],
	['src/lib/components/rb/wave/WaveRow.svelte', ['⏏']],
	// Spelled as JS escapes / HTML entities, so a literal search misses them.
	['src/lib/components/rb/browser/AutoPlayWalkthrough.svelte', ['\u{1F60A}', '\u{1F643}']],
	['src/lib/components/rb/deck/HotCueBank.svelte', ['✎']],
	['src/routes/progress-tree/DepGraph.svelte', ['⚠', '↗']],
	['src/routes/progress-tree/NodeDetail.svelte', ['↗']]
]);

function stripComments(src) {
	return src
		.replace(/<!--[\s\S]*?-->/g, '')
		.replace(/\/\*[\s\S]*?\*\//g, '')
		.replace(/(^|[^:\\'"`])\/\/[^\n]*/g, '$1');
}

const ENCODED_RE = /&#(\d+);|&#[xX]([0-9a-fA-F]+);|\\u\{([0-9a-fA-F]+)\}|\\u([0-9a-fA-F]{4})/g;

function decodeEncoded(src) {
	return src.replace(ENCODED_RE, (_m, dec, hex, braced, u4) =>
		dec !== undefined
			? String.fromCodePoint(Number(dec))
			: String.fromCodePoint(parseInt(hex ?? braced ?? u4, 16))
	);
}

function scannable(src) {
	return decodeEncoded(stripComments(src));
}

function emojiIn(text) {
	return [...new Set([...text.matchAll(EMOJI_RE)].map((m) => m[0]))].filter(
		(c) => !TEXT_SYMBOLS.has(c)
	);
}

function bannedIn(text) {
	return TOPBAR_BANNED_GLYPHS.filter((g) => text.includes(g));
}

function rel(abs) {
	return relative(FRONTEND, abs).split('\\').join('/');
}

/** TopBar.svelte plus every .svelte it imports, transitively, that lives
 * under src/lib/components (the top bar's own child tree). */
function topBarFiles() {
	const start = join(LIB, 'components/rb/TopBar.svelte');
	const seen = new Set();
	const queue = [start];
	const importRe = /import\s+[^'"]*?from\s+['"]([^'"]+\.svelte)['"]/g;
	while (queue.length > 0) {
		const file = queue.shift();
		if (seen.has(file)) continue;
		seen.add(file);
		const src = readFileSync(file, 'utf8');
		for (const [, spec] of src.matchAll(importRe)) {
			const target = spec.startsWith('$lib/')
				? join(LIB, spec.slice('$lib/'.length))
				: spec.startsWith('.')
					? resolve(dirname(file), spec)
					: null;
			if (target === null || !existsSync(target)) continue;
			if (!rel(target).startsWith('src/lib/components/')) continue;
			queue.push(target);
		}
	}
	return [...seen, join(LIB, 'components/rb/midi/midi-format.ts')];
}

function walkSvelte(dir, out = []) {
	for (const name of readdirSync(dir)) {
		const p = join(dir, name);
		if (statSync(p).isDirectory()) walkSvelte(p, out);
		else if (name.endsWith('.svelte')) out.push(p);
	}
	return out;
}

test('positive controls: each detector fires on a planted offender, and comments are ignored', () => {
	assert.deepEqual(emojiIn(scannable('<span>\u2705 done</span>')), ['\u2705']);
	assert.deepEqual(emojiIn(scannable('<span>&#x1F916;</span>')), ['\u{1F916}']);
	assert.deepEqual(emojiIn(scannable("const s = '\\u{1F3B5}';")), ['\u{1F3B5}']);
	assert.deepEqual(bannedIn(scannable('<b>MIDI \u2713</b>')), ['\u2713']);
	assert.deepEqual(emojiIn(scannable('<!-- \u2705 --> /* \u2705 */ // \u2705\n<a href="https://x.test">ok</a>')), []);
	assert.deepEqual(emojiIn(scannable('<p>F\u266Fm</p>')), [], 'musical accidentals are text');
});

test('the top bar tree is actually measured (not an empty file set)', () => {
	const files = topBarFiles().map(rel);
	assert.ok(files.includes('src/lib/components/rb/TopBar.svelte'));
	// Children the top bar renders today; if the import walk breaks, these vanish.
	// The MIDI button left the top bar for the mixer I/O row (CHROME-07, Preview integration).
	for (const child of ['src/lib/components/UserBauble.svelte', 'src/lib/components/rb/midi/MidiPanelLoader.svelte']) {
		assert.ok(files.includes(child), `${child} missing from the top bar sweep: ${files.join(', ')}`);
	}
});

test('#3886: the top bar renders no emoji and no text-symbol stand-in icons', () => {
	const offenders = [];
	for (const file of topBarFiles()) {
		const text = scannable(readFileSync(file, 'utf8'));
		const hits = [...emojiIn(text), ...bannedIn(text)];
		if (hits.length > 0) offenders.push(`${rel(file)}: ${[...new Set(hits)].join(' ')}`);
	}
	assert.deepEqual(offenders, [], `use an inline SVG icon instead:\n${offenders.join('\n')}`);
});

test('#3886: the MIDI status renders SVG tick / cross icons (CHROME-07 moved it to the mixer I/O row)', () => {
	const glyph = readFileSync(join(LIB, 'components/rb/mixer/MidiStatusGlyph.svelte'), 'utf8');
	assert.match(glyph, /<svg\s+class="midi-glyph"/);
	assert.match(glyph, /glyph === 'tick' \? MIDI_TICK_PATH : MIDI_CROSS_PATH/);
	assert.deepEqual([...emojiIn(scannable(glyph)), ...bannedIn(scannable(glyph))], []);
	const cluster = readFileSync(join(LIB, 'components/rb/mixer/HeadphoneCluster.svelte'), 'utf8');
	assert.match(cluster, />MIDI<MidiStatusGlyph glyph=\{midiGlyph\} \/>/);
});

test('#3886: no NEW emoji anywhere in UI markup (src/lib/components, src/routes)', () => {
	const files = [
		...walkSvelte(join(FRONTEND, 'src/lib/components')),
		...walkSvelte(join(FRONTEND, 'src/routes'))
	];
	assert.ok(files.length > 100, `expected the whole UI tree, saw ${files.length} files`);
	const offenders = [];
	for (const file of files) {
		const allowed = new Set(KNOWN_DEBT.get(rel(file)) ?? []);
		const hits = emojiIn(scannable(readFileSync(file, 'utf8'))).filter((c) => !allowed.has(c));
		if (hits.length > 0) offenders.push(`${rel(file)}: ${hits.join(' ')}`);
	}
	assert.deepEqual(offenders, [], `use an inline SVG icon instead:\n${offenders.join('\n')}`);
});

test('KNOWN_DEBT names only files that still exist', () => {
	for (const file of KNOWN_DEBT.keys()) {
		assert.ok(existsSync(join(FRONTEND, file)), `${file} is gone: delete its KNOWN_DEBT entry`);
	}
});
