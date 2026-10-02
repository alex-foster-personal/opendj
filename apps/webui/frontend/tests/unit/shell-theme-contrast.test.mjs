/**
 * The app shell (app.css) honors the `theme` setting on every route, not
 * only /performance: prefs.svelte.ts writes html[data-theme] globally, and
 * app.css answers html[data-theme='light'] with a light value for every
 * shell token.
 *
 * Regression lines:
 * - if app.css has no light block then every non-performance route stays
 *   dark while the setting says "light" (the bug this guards)
 * - if a shell token is declared dark-only then the light block has a hole
 *   that renders a dark value on a light page
 * - if a light (or dark) text/surface pair drops under 4.5:1, or an
 *   indicator under 3:1, the shell is unreadable in that theme
 * - if the shell's own rules go back to a hard-coded dark hex then the light
 *   block cannot reach them
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

let cc;
before(async () => {
	cc = await loadTypeScriptModule('src/lib/rb/color-contrast.ts');
});

const read = (rel) => readFileSync(fileURLToPath(new URL(`../../src/${rel}`, import.meta.url)), 'utf8');
const APP_CSS = read('app.css');
const SELECTORS = { dark: ':root', light: "html[data-theme='light']" };

/** Every shell pairing that a real rule in app.css (or a route reading the
 * shell tokens) draws. */
const SHELL_PAIRINGS = [
	['fg', 'bg', 'body', 'body text on page'],
	['fg', 'surface', 'body', 'body text on sidebar/topbar/panels'],
	['fg', 'surface-raised', 'body', 'button and active-link text'],
	['fg', 'surface-raised-hover', 'body', 'hovered button text'],
	['fg', 'row-hover', 'body', 'hovered library row text'],
	['muted', 'bg', 'body', 'muted text on page'],
	['muted', 'surface', 'body', 'muted text on panels'],
	['muted', 'surface-raised', 'body', 'chip text'],
	['accent', 'bg', 'body', 'links and eyebrows on page'],
	['accent', 'surface', 'body', 'sidebar title, accent text on panels'],
	['on-accent', 'accent', 'body', 'primary button text'],
	['on-danger', 'danger', 'body', 'danger banner text'],
	['danger', 'surface', 'body', 'danger text on panels'],
	['warning', 'bg', 'non-text', 'warning toast border'],
	['accent-dim', 'bg', 'non-text', 'empty rating star'],
	['success', 'bg', 'body', 'success chip text'],
	['success', 'surface', 'body', 'success chip text on panels'],
	['on-info', 'info', 'body', 'share button text']
].map(([fg, bg, level, label]) => ({ fg, bg, level, label }));

for (const [theme, selector] of Object.entries(SELECTORS)) {
	test(`${theme} shell tokens meet every stated WCAG floor`, () => {
		const tokens = cc.parseColorTokens(APP_CSS, selector);
		const violations = cc.validateScheme(tokens, SHELL_PAIRINGS);
		assert.deepEqual(violations, [], cc.describeViolations(violations));
	});
}

test('light block redefines every color token the dark :root declares', () => {
	const dark = cc.parseColorTokens(APP_CSS, ':root');
	const light = cc.parseColorTokens(APP_CSS, "html[data-theme='light']");
	assert.ok(Object.keys(dark).length >= 15, 'the dark parse found the shell palette');
	const missing = Object.keys(dark).filter((name) => !(name in light));
	assert.deepEqual(missing, [], `dark-only shell tokens: ${missing}`);
	assert.notEqual(light.bg, dark.bg, 'light --bg is actually light, not a copy');
	assert.match(APP_CSS, /html\[data-theme='light'\] \{[^}]*color-scheme: light;/);
});

test('control: the dark text color on the light background fails (the check can say no)', () => {
	const dark = cc.parseColorTokens(APP_CSS, ':root');
	const light = cc.parseColorTokens(APP_CSS, "html[data-theme='light']");
	const broken = { ...light, fg: dark.fg };
	assert.ok(cc.validateScheme(broken, SHELL_PAIRINGS).length > 0);
});

test('shell rules outside the token blocks carry no hard-coded hex color', () => {
	const withoutTokenBlocks = APP_CSS.replace(/:root \{[^}]*\}/, '').replace(
		/html\[data-theme='light'\] \{[^}]*\}/,
		''
	);
	const literals = withoutTokenBlocks.match(/#[0-9a-fA-F]{3,6}\b/g) ?? [];
	assert.deepEqual(literals, []);
});

test('the theme attribute is applied app-wide, not only on /performance', () => {
	const prefs = read('lib/rb/prefs.svelte.ts');
	assert.match(prefs, /document\.documentElement\.dataset\.theme = theme;/);
	const settings = read('lib/settings/catalog.ts');
	assert.match(settings, /Applies to the shell and \/performance/);
});

for (const route of ['play-analytics', 'sets', 'dedup']) {
	test(`routes/${route} styles read shell tokens, no hard-coded hex`, () => {
		const src = read(`routes/${route}/+page.svelte`);
		const style = src.slice(src.indexOf('<style'));
		assert.ok(style.includes('var(--'), `${route} styles use tokens`);
		assert.deepEqual(style.match(/#[0-9a-fA-F]{3,6}\b/g) ?? [], []);
	});
}
