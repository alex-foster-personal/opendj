/**
 * LIBUX-50 (W16, the maintainer UI nit 6, Tue 6 Oct 2026): "loading bar space is still
 * there when not loading. Loading bar is blue in gothic-skin not white".
 *
 * Two contracts on LibraryLoadIndicator.svelte, each checked by a pure
 * function that returns its failures, so every check can be run against the
 * real source AND against mutated copies of it in the same test:
 *
 * - idle reserves no space: the root is a zero-height, always-mounted
 *   role=status anchor; the strip is an absolutely positioned child, so it
 *   is out of flow whether or not it renders.
 * - the color comes from the skin: the track and bar paint var(--lli-ink),
 *   --lli-ink is the skin accent --rb-accent, no hex literal remains in the
 *   component, and theme.css gives Gothic (mono-dev) a gray accent while
 *   Default keeps a blue one.
 *
 * The browser-measured half (root height 0 idle and loading, bar
 * rgb(214, 214, 214) on Gothic and rgb(47, 111, 214) on Default, against
 * 18px and rgb(79, 178, 255) on both before) is in the PR, not here: this
 * suite has no DOM.
 *
 * - if the root takes height again then idle shows the empty band -> broken
 * - if the strip goes back into normal flow then every load start and end
 *   moves the column headers (pin 02717d4ea496) -> broken
 * - if the bar or track paints a literal color then Gothic stays blue -> broken
 * - if --lli-ink stops following --rb-accent, or theme.css stops graying
 *   the Gothic accent, then Gothic stays blue -> broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const indicatorSrc = readFileSync(
	new URL('../../src/lib/components/rb/browser/LibraryLoadIndicator.svelte', import.meta.url),
	'utf8'
);
const themeSrc = readFileSync(new URL('../../src/lib/rb/theme.css', import.meta.url), 'utf8');

function stripComments(src) {
	return src
		.replace(/<!--[\s\S]*?-->/g, '')
		.replace(/\/\*[\s\S]*?\*\//g, '')
		.replace(/\/\/.*$/gm, '');
}

function cssRule(src, selector) {
	const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
	const match = new RegExp(`(?:^|\\n)\\s*${escaped} \\{([\\s\\S]*?)\\n\\s*\\}`).exec(src);
	return match ? match[1] : null;
}

function idleReservesNoSpaceFailures(componentSrc) {
	const failures = [];
	const src = stripComments(componentSrc);
	const markup = src.slice(src.indexOf('</script>'), src.indexOf('<style>'));
	const style = src.slice(src.indexOf('<style>'));
	if (!/^<\/script>\s*<div class="lli-root" role="status" aria-live="polite">\s*\{#if /.test(markup)) {
		failures.push('root is not an unconditional role=status element wrapping the conditional');
	}
	const root = cssRule(style, '.lli-root');
	if (root === null) return [...failures, 'no .lli-root rule'];
	if (!/(?:^|\n)\s*height: 0;/.test(root)) failures.push('root height is not 0');
	if (!/flex: 0 0 0;/.test(root)) failures.push('root flex-basis is not 0');
	if (/(?:^|\n)\s*(?:min-height|padding|border|margin)(?:-[a-z]+)?:/.test(root)) {
		failures.push('root declares a box metric that can take space');
	}
	const strip = cssRule(style, '.lli-strip');
	if (strip === null) return [...failures, 'no .lli-strip rule'];
	if (!/position: absolute;/.test(strip)) failures.push('strip is in normal flow');
	return failures;
}

function inkFromSkinFailures(componentSrc, theme) {
	const failures = [];
	const style = stripComments(componentSrc.slice(componentSrc.indexOf('<style>')));
	const hex = style.match(/#[0-9a-fA-F]{3,8}\b/g);
	if (hex) failures.push(`literal colors in the style block: ${hex.join(', ')}`);
	const root = cssRule(style, '.lli-root') ?? '';
	if (!/--lli-ink: var\(--rb-accent\);/.test(root)) failures.push('--lli-ink is not the skin accent');
	const bar = cssRule(style, '.lli-bar') ?? '';
	if (!/background: var\(--lli-ink\);/.test(bar)) failures.push('bar does not paint --lli-ink');
	const track = cssRule(style, '.lli-track') ?? '';
	if (!/background: color-mix\(in srgb, var\(--lli-ink\)/.test(track)) failures.push('track fill does not use --lli-ink');
	if (!/outline: 1px solid color-mix\(in srgb, var\(--lli-ink\)/.test(track)) failures.push('track end does not use --lli-ink');

	const accent = (selector) => {
		const rule = cssRule(stripComments(theme), selector);
		const m = rule && /--rb-accent: #([0-9a-fA-F]{6});/.exec(rule);
		return m ? [0, 2, 4].map((i) => parseInt(m[1].slice(i, i + 2), 16)) : null;
	};
	const gothic = accent("html[data-skin='mono-dev'] .perf-root");
	if (gothic === null) failures.push('theme.css gives Gothic no --rb-accent');
	else if (!(gothic[0] === gothic[1] && gothic[1] === gothic[2] && gothic[0] >= 0xb0)) {
		failures.push(`Gothic accent is not a light gray: rgb(${gothic.join(', ')})`);
	}
	const dflt = accent('.perf-root');
	if (dflt === null) failures.push('theme.css gives Default no --rb-accent');
	else if (!(dflt[2] > dflt[0] && dflt[2] > dflt[1])) failures.push('Default accent is not blue');
	return failures;
}

/** Each mutation must change the source, or it proves nothing. */
function mutate(src, from, to) {
	const out = src.replace(from, to);
	assert.notEqual(out, src, `mutation did not apply: ${from}`);
	return out;
}

test('LIBUX-50: idle reserves no space (root zero-height and mounted, strip out of flow)', () => {
	assert.deepEqual(idleReservesNoSpaceFailures(indicatorSrc), []);
});

test('LIBUX-50 mutation check: each way back to an idle gap or a layout jump goes red', () => {
	const mutants = {
		'root back to the 18px reserved height': mutate(indicatorSrc, /(\.lli-root \{[\s\S]*?)height: 0;/, '$1height: var(--lli-strip-h);'),
		'root flex-basis back to 18px': mutate(indicatorSrc, 'flex: 0 0 0;', 'flex: 0 0 var(--lli-strip-h);'),
		'root takes padding': mutate(indicatorSrc, /(\.lli-root \{\n)/, '$1\t\tpadding: 0 8px;\n'),
		'strip in normal flow': mutate(indicatorSrc, /(\.lli-strip \{[\s\S]*?)position: absolute;/, '$1position: relative;'),
		'root mounted only while loading': mutate(
			indicatorSrc,
			'<div class="lli-root" role="status" aria-live="polite">\n\t{#if loading || progress !== null || searching}',
			'{#if loading || progress !== null || searching}\n<div class="lli-root" role="status" aria-live="polite">'
		)
	};
	for (const [name, src] of Object.entries(mutants)) {
		assert.notDeepEqual(idleReservesNoSpaceFailures(src), [], `mutant stayed green: ${name}`);
	}
});

test('LIBUX-50: the bar color resolves from the skin accent, gray on Gothic and blue on Default', () => {
	assert.deepEqual(inkFromSkinFailures(indicatorSrc, themeSrc), []);
});

test('LIBUX-50 mutation check: each way back to a fixed blue bar goes red', () => {
	const mutants = {
		'bar paints the old literal': [mutate(indicatorSrc, 'background: var(--lli-ink);', 'background: #4fb2ff;'), themeSrc],
		'ink pinned to a literal': [mutate(indicatorSrc, '--lli-ink: var(--rb-accent);', '--lli-ink: #4fb2ff;'), themeSrc],
		'ink follows the waveform vocal color': [mutate(indicatorSrc, '--lli-ink: var(--rb-accent);', '--lli-ink: var(--rb-wave-vocal);'), themeSrc],
		'track fill paints the old literal': [
			mutate(indicatorSrc, 'background: color-mix(in srgb, var(--lli-ink) 18%', 'background: color-mix(in srgb, #4fb2ff 18%'),
			themeSrc
		],
		'Gothic accent turned blue': [indicatorSrc, mutate(themeSrc, /(html\[data-skin='mono-dev'\] \.perf-root \{[\s\S]*?)--rb-accent: #d6d6d6;/, '$1--rb-accent: #4fb2ff;')]
	};
	for (const [name, [src, theme]] of Object.entries(mutants)) {
		assert.notDeepEqual(inkFromSkinFailures(src, theme), [], `mutant stayed green: ${name}`);
	}
});

let mod;
before(async () => {
	mod = await loadSvelteSsrModule(
		[
			"export { default as Indicator } from '$lib/components/rb/browser/LibraryLoadIndicator.svelte';",
			"export { render } from 'svelte/server';"
		].join('\n')
	);
});

test('LIBUX-50: idle renders the status root alone; a load renders the strip inside it', () => {
	const idle = mod.render(mod.Indicator, { props: { loading: false, progress: null } }).body;
	assert.match(idle, /<div class="lli-root[^"]*" role="status" aria-live="polite">/);
	assert.equal(idle.includes('lli-strip'), false, `idle rendered the strip: ${idle}`);
	const loading = mod.render(mod.Indicator, { props: { loading: true, progress: null } }).body;
	assert.match(loading, /<div class="lli-root[^"]*" role="status"[^>]*>(?:<!--[^>]*-->)*<div class="lli-strip/);
});

/**
 * Sol P2 on #5657 (debt-logged in (private path)): the overlay strip
 * ignores the pointer so header clicks fall through, but the progress track
 * and the rows/s figure carry hover titles (house rule: numeric readouts have
 * a title), so those two must take the pointer back.
 *
 * - if the track or the rate inherits pointer-events: none then their titles
 *   never show while loading -> broken
 * - if the whole strip takes the pointer then it swallows clicks on the
 *   column-header row it covers (the overshoot) -> broken
 */
function hoverTitleFailures(componentSrc) {
	const failures = [];
	const src = stripComments(componentSrc);
	const markup = src.slice(src.indexOf('</script>'), src.indexOf('<style>'));
	const style = src.slice(src.indexOf('<style>'));
	if (!/class="lli-track"[\s\S]*?title=\{/.test(markup)) failures.push('track has no title');
	if (!/class="lli-rate" title="/.test(markup)) failures.push('rows/s has no title');
	if (!/pointer-events: none;/.test(cssRule(style, '.lli-strip') ?? '')) {
		failures.push('strip takes the pointer and would block header clicks');
	}
	for (const sel of ['.lli-track', '.lli-rate']) {
		if (!/pointer-events: auto;/.test(cssRule(style, sel) ?? '')) {
			failures.push(`${sel} does not take the pointer back, so its title cannot show`);
		}
	}
	return failures;
}

test('LIBUX-50: the titled readouts take hover while header clicks fall through the strip', () => {
	assert.deepEqual(hoverTitleFailures(indicatorSrc), []);
});

test('LIBUX-50 mutation check: losing either hover target, or the whole strip taking the pointer, goes red', () => {
	const mutants = {
		'track inherits none': mutate(indicatorSrc, /(\.lli-track \{[\s\S]*?)\t\tpointer-events: auto;\n/, '$1'),
		'rate inherits none': mutate(indicatorSrc, /(\.lli-rate \{[\s\S]*?)\t\tpointer-events: auto;\n/, '$1'),
		'whole strip takes the pointer': mutate(indicatorSrc, /(\.lli-strip \{[\s\S]*?)pointer-events: none;/, '$1pointer-events: auto;')
	};
	for (const [name, src] of Object.entries(mutants)) {
		assert.notDeepEqual(hoverTitleFailures(src), [], `mutant stayed green: ${name}`);
	}
});
