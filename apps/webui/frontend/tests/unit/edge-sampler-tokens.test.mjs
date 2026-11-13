/**
 * Issue #13723: the edge sampler draws from `--es-*` tokens carrying the approved
 * mock's own colours, declared once in edge-sampler-tokens.css.
 *
 * Regression lines:
 * - if a mock colour is missing from the token stylesheet or differs then the sampler drifts from the mock
 * - if a token names a font family the app does not bundle then it silently falls back
 * - if a sampler component's rendered stylesheet carries a literal colour then it bypasses the tokens
 */
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const fromFrontend = (rel) => fileURLToPath(new URL(`../../${rel}`, import.meta.url));
const MOCK_HTML = readFileSync(
	fromFrontend('../../../.planning/research/edge-sampler-20261109/edge-sampler-mocks.html'),
	'utf8'
);
const MOCK_CSS = MOCK_HTML.slice(MOCK_HTML.indexOf('<style>'), MOCK_HTML.indexOf('</style>'));
const TOKENS_CSS = readFileSync(
	fromFrontend('src/lib/components/rb/sampler/edge-sampler-tokens.css'),
	'utf8'
);
const COLOR_RE = /#[0-9a-f]{3,8}\b|rgba?\([^)]*\)|hsla?\([^)]*\)/gi;
// The mock's demo page chrome (its help strip), not part of the sampler.
const DEMO_ONLY = new Set(['rgba(17,20,25,.92)']);

/** One spelling per colour or font stack: no spaces, `0.5` as `.5`, double quotes as single. */
const norm = (v) =>
	v
		.trim()
		.toLowerCase()
		.replace(/\s+/g, '')
		.replace(/([(,])0\./g, '$1.')
		.replaceAll('"', "'");

/** `--name: value;` pairs of the first `selector {` block starting a line, values normalised. */
function declarations(css, selector) {
	const start = `\n${css}`.indexOf(`\n${selector} {`);
	assert.notEqual(start, -1, `selector ${selector} present`);
	const body = css.slice(start, css.indexOf('}', start));
	return Object.fromEntries(
		[...body.matchAll(/--([\w-]+):\s*([^;]+);/g)].map((m) => [m[1], norm(m[2])])
	);
}

const TOKENS = declarations(TOKENS_CSS, ':root');
const mockName = (name) => `es-${name.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`;

let ssr;
before(async () => {
	ssr = await loadSvelteSsrModule(
		[
			"export { default as EdgeWave } from '$lib/components/rb/sampler/EdgeWave.svelte';",
			"export { default as EdgeSamplerToggle } from '$lib/components/rb/sampler/EdgeSamplerToggle.svelte';",
			"export { DEFAULT_WAVE } from '$lib/player/sampler/edge-wave';",
			"export { render } from 'svelte/server';"
		].join('\n'),
		{ css: 'injected' }
	);
});

/** Scoped CSS svelte emits into `head` for one rendered component. */
const renderedCss = (component, props) => {
	const { head, body } = ssr.render(component, { props });
	return {
		css: [...head.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1]).join('\n'),
		body
	};
};

test('every mock :root colour is an --es- token with the same value', () => {
	/** [if] a mock :root colour var is listed [then] es-<name> in the token stylesheet equals it, [else stop]. */
	const colors = Object.entries(declarations(MOCK_CSS, ':root')).filter(
		([n]) => !n.startsWith('f-')
	);
	assert.equal(colors.length, 14);
	for (const [name, value] of colors) assert.equal(TOKENS[mockName(name)], value, name);
});

test('every literal colour in the mock sampler CSS is an --es- token value', () => {
	/** [if] a colour literal appears in the mock's style block [then] some --es- token carries that value, [else stop]. */
	const values = new Set(Object.values(TOKENS));
	const literals = new Set([...MOCK_CSS.matchAll(COLOR_RE)].map((m) => norm(m[0])));
	const missing = [...literals].filter((c) => !DEMO_ONLY.has(c) && !values.has(c));
	assert.deepEqual(missing, []);
	assert.ok(literals.size >= 35, `parsed ${literals.size} mock colours`);
});

test('font tokens name no family the app does not bundle', () => {
	/** [if] the mock's Barlow or IBM Plex families appear in a font token [then] fail, the app ships neither, [else stop]. */
	const fonts = Object.entries(TOKENS).filter(([n]) => n.startsWith('es-font-'));
	assert.deepEqual(fonts.map(([n]) => n).sort(), ['es-font-display', 'es-font-mono']);
	for (const [, stack] of fonts) assert.doesNotMatch(stack, /barlow|ibmplex/);
	assert.equal(declarations(TOKENS_CSS, '.perf-root')['es-font-body'], 'var(--rb-font)');
});

// The light theme and every non-default skin (mono-dev, light, ...) re-colour the app
// through --rb-* on .perf-root (src/lib/rb/theme.css); the sampler follows them there.
const FOLLOW_THEME = "html:is([data-theme='light'], [data-skin]) .perf-root";
const THEME_CSS = readFileSync(fromFrontend('src/lib/rb/theme.css'), 'utf8');
// A black drop shadow reads on every surface, so it stays the mock's in every theme.
const themeColour = (name) => !/^es-(font|shadow)-/.test(name);

test('every --es- colour follows the light theme and the skins through --rb- vars', () => {
	/** [if] html[data-theme='light'] or a data-skin is set [then] each --es- colour reads a --rb- theme var the default palette declares, [else stop]. */
	const follow = declarations(TOKENS_CSS, FOLLOW_THEME);
	const rbDefault = declarations(THEME_CSS, '.perf-root');
	const colours = Object.keys(TOKENS).filter(themeColour);
	assert.ok(colours.length >= 30, `${colours.length} colour tokens`);
	for (const name of colours) {
		const refs = [...(follow[name] ?? '').matchAll(/var\(--(rb-[\w-]+)\)/g)].map((m) => m[1]);
		assert.ok(refs.length > 0, `--${name} reads a --rb- theme var under the light theme and skins`);
		for (const rb of refs) assert.ok(rb in rbDefault, `--${rb} is a theme.css palette var`);
	}
	// Every --es- colour a sampler component reads is one of them.
	const dir = 'src/lib/components/rb/sampler';
	const read = readdirSync(fromFrontend(dir))
		.filter((f) => f.endsWith('.svelte'))
		.flatMap((f) => [
			...readFileSync(fromFrontend(`${dir}/${f}`), 'utf8').matchAll(/var\(--(es-[\w-]+)\)/g)
		])
		.map((m) => m[1]);
	assert.ok(read.length > 0, 'components read --es- tokens');
	for (const name of new Set(read.filter(themeColour))) assert.ok(name in follow, `--${name}`);
});

test('under the light theme and mono skin the sampler text, accent and tab take the theme colours', () => {
	/** [if] the light theme or mono-dev skin is on [then] --es-fg/muted/accent/head-bg/tab-border resolve to that palette, not the dark mock hex, [else stop]. */
	const follow = declarations(TOKENS_CSS, FOLLOW_THEME);
	for (const selector of [
		"html[data-theme='light'] .perf-root",
		"html[data-skin='mono-dev'] .perf-root"
	]) {
		const palette = declarations(THEME_CSS, selector);
		for (const name of ['es-fg', 'es-muted', 'es-accent', 'es-head-bg', 'es-tab-border']) {
			const rb = /^var\(--(rb-[\w-]+)\)$/.exec(follow[name] ?? '')?.[1];
			assert.ok(rb && palette[rb], `${selector}: --${name} reads one --rb- var it sets`);
			assert.notEqual(
				palette[rb],
				TOKENS[name],
				`${selector}: --${name} differs from the dark mock`
			);
		}
	}
});

test('rendered edge wave and top tab paint the empty state from tokens, never a literal colour', () => {
	/** [if] EdgeWave renders with empty slots [then] its CSS and its top tab's hold no colour literal or --rb- var, [else stop]. */
	const props = {
		edge: 'L',
		top: 28,
		height: 600,
		params: ssr.DEFAULT_WAVE,
		enabled: true,
		reduceMotion: true
	};
	const { css, body } = renderedCss(ssr.EdgeWave, props);
	assert.match(body, /class="wslot[^"]*\bempty\b/, 'an empty slot rendered');
	assert.match(body, /data-testid="edge-wave-top-L"/, 'the top tab rendered');
	assert.match(css, /\.wslot\.empty[^{]*\{[^}]*var\(--es-border-strong\)[^}]*var\(--es-empty-bg\)/);
	assert.match(css, /\.whead[^{]*\{[^}]*var\(--es-head-bg\)/);
	assert.deepEqual(css.match(COLOR_RE) ?? [], []);
	assert.deepEqual(css.match(/var\(--rb-[\w-]+\)/g) ?? [], []);
});

test('rendered sampler toggle paints its on and off states from tokens', () => {
	/** [if] the sampler chip renders [then] on reads --es-accent, off --es-muted, box from --es- tokens, no literal or --rb-, [else stop]. */
	const { css } = renderedCss(ssr.EdgeSamplerToggle, {});
	assert.match(css, /\.sampler-on[^{]*\{[^}]*var\(--es-accent\)/);
	assert.match(css, /var\(--es-muted\)/);
	for (const token of ['border-strong', 'head-bg', 'ghost-bg', 'toggle-ring'])
		assert.match(css, new RegExp(`var\\(--es-${token}\\)`), `chip reads --es-${token}`);
	assert.deepEqual(css.match(COLOR_RE) ?? [], []);
	assert.deepEqual(css.match(/var\(--rb-[\w-]+\)/g) ?? [], []);
});
