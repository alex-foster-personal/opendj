/**
 * A11Y-11403 source scan: every `outline: none` under src/lib/components is
 * paired, in the same file, with a `:focus-visible` rule that draws a ring of
 * at least 2px on the one app-wide token, `--rb-focus-ring`.
 *
 * Rule parsing is the flat `selector { body }` regex the contrast test uses:
 * it sees through `:global(...)` and the inner rules of `@media` blocks, but
 * not nested CSS. Good enough for the component styles in this tree.
 *
 * Regression lines:
 *   if a component resets outline with no 2px --rb-focus-ring :focus-visible rule then broken
 *   if the scanner stops finding resets (TrackTable, Knob) then broken
 *   if app.css loses the global :focus-visible ring or :root loses --rb-focus-ring then broken
 *   if anything under src still reads --rb-focus-outline then broken
 */
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { join, relative, sep } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));
const COMPONENTS = join(SRC, 'lib', 'components');

//------------------------------------------------------------------ scanner
const RESET = /outline\s*:\s*none\s*(?:!important)?\s*[;}]/i;
const RULE = /([^{}]+)\{([^{}]*)\}/g;
const OUTLINE_RING =
	/outline(?:-width)?\s*:[^;}]*?\b(\d+(?:\.\d+)?)px[^;}]*var\(--rb-focus-ring\b/i;
const OUTLINE_RING_VAR_FIRST = /outline\s*:[^;}]*var\(--rb-focus-ring\b[^;}]*?\b(\d+(?:\.\d+)?)px/i;
const SHADOW_RING =
	/box-shadow\s*:[^;}]*\b0(?:px)?\s+0(?:px)?\s+0(?:px)?\s+(\d+(?:\.\d+)?)px[^;}]*var\(--rb-focus-ring\b/i;

/** The CSS a file contributes: `<style>` bodies of a .svelte, the whole of a .css, comments stripped. */
function styleBlocksOf(source, path) {
	const css = path.endsWith('.svelte')
		? [...source.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1]).join('\n')
		: source;
	return css.replace(/\/\*[\s\S]*?\*\//g, '');
}

function hasOutlineReset(css) {
	return RESET.test(css);
}

function hasFocusRing(css) {
	for (const [, selector, body] of css.matchAll(RULE)) {
		if (!selector.includes(':focus-visible')) continue;
		for (const re of [OUTLINE_RING, OUTLINE_RING_VAR_FIRST, SHADOW_RING]) {
			const m = body.match(re);
			if (m && Number(m[1]) >= 2) return true;
		}
	}
	return false;
}

function walk(dir) {
	return readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
		e.isDirectory()
			? walk(join(dir, e.name))
			: /\.(svelte|css)$/.test(e.name)
				? [join(dir, e.name)]
				: []
	);
}

function scanComponents() {
	const resetFiles = [];
	const offenders = [];
	for (const path of walk(COMPONENTS)) {
		const css = styleBlocksOf(readFileSync(path, 'utf8'), path);
		if (!hasOutlineReset(css)) continue;
		const rel = relative(COMPONENTS, path).split(sep).join('/');
		resetFiles.push(rel);
		if (!hasFocusRing(css)) offenders.push(rel);
	}
	return { resetFiles, offenders };
}

//------------------------------------------------------------------ tests
test('if a component resets outline with no 2px --rb-focus-ring :focus-visible rule then keyboard focus is lost', () => {
	const { offenders } = scanComponents();
	assert.deepEqual(
		offenders,
		[],
		'add a :focus-visible rule in the same file: outline: 2px solid var(--rb-focus-ring); outline-offset: 2px'
	);
});

test('if the scanner stops finding resets then it is blind', () => {
	const { resetFiles } = scanComponents();
	assert.ok(resetFiles.length >= 20, `only ${resetFiles.length} reset files found`);
	for (const f of [
		'rb/browser/TrackTable.svelte',
		'rb/mixer/Knob.svelte',
		'rb/wave/WaveRow.chrome.css'
	]) {
		assert.ok(resetFiles.includes(f), `${f} not seen as a reset file`);
	}
});

test('if the scanner accepts a reset with no real 2px token ring then it is too lax', () => {
	const css = (s) => styleBlocksOf(`<style>${s}</style>`, 'x.svelte');
	const fails = [
		'.a{outline:none}',
		'.a{outline: none !important;} .a:focus{outline: 2px solid var(--rb-focus-ring);}',
		'.a{outline: none;} .a:focus-visible{outline: 1px solid var(--rb-focus-ring);}',
		'.a{outline: none;} .a:focus-visible{outline: 2px solid var(--rb-accent);}',
		'.a{outline: none;} /* .a:focus-visible{outline: 2px solid var(--rb-focus-ring);} */'
	];
	for (const s of fails) {
		assert.ok(hasOutlineReset(css(s)), `reset not detected: ${s}`);
		assert.equal(hasFocusRing(css(s)), false, `wrongly paired: ${s}`);
	}
	const passes = [
		'.a{outline: none;} .a:focus-visible{box-shadow: 0 0 0 2px var(--rb-focus-ring);}',
		'tr{outline: none !important;} tr:focus-visible{outline: 2px solid var(--rb-focus-ring) !important;}'
	];
	for (const s of passes) assert.ok(hasFocusRing(css(s)), `pairing missed: ${s}`);
});

test('if app.css loses the global :focus-visible ring or the shell token then the ring vanishes off /performance', () => {
	const app = readFileSync(join(SRC, 'app.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
	const global = [...app.matchAll(RULE)].find(([, sel]) => sel.trim() === ':focus-visible');
	assert.ok(global, 'app.css has no top-level :focus-visible rule');
	assert.match(global[2], /outline:\s*2px solid var\(--rb-focus-ring\);/);
	assert.match(global[2], /outline-offset:\s*2px;/);
	const root = [...app.matchAll(RULE)].find(([, sel]) => sel.trim() === ':root');
	assert.match(root[2], /--rb-focus-ring:\s*#[0-9a-f]{6}/i);
});

test('if anything under src still reads --rb-focus-outline then the retired 1px token is back', () => {
	const all = (dir) =>
		readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
			e.isDirectory()
				? all(join(dir, e.name))
				: /\.(svelte|css|ts|js)$/.test(e.name)
					? [join(dir, e.name)]
					: []
		);
	const hits = all(SRC).filter((p) => readFileSync(p, 'utf8').includes('--rb-focus-outline'));
	assert.deepEqual(
		hits.map((p) => relative(SRC, p)),
		[]
	);
});
