// requirement: LYR-12, UX-MENU-01
// Issue #13352 ((private path), Goal): the LYR trigger is the same
// real control in every skin, so the Gothic (mono-dev) skin must not own its colors,
// must not detach it from the lyrics action, and must never disable it.
// [if] the Gothic skin stylesheet changes LYR colors [then] this file fails, [else stop].
// [if] the LYR control loses its lyrics action binding [then] this file fails, [else stop].
// [if] the control is disabled for unavailable lyrics [then] this file fails, [else stop].
import assert from 'node:assert/strict';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND = fileURLToPath(new URL('../..', import.meta.url));
const SRC = join(FRONTEND, 'src');
const TOPBAR = readFileSync(join(SRC, 'lib/components/rb/TopBar.svelte'), 'utf8');
const MARKUP = TOPBAR.slice(TOPBAR.indexOf('</script>'), TOPBAR.indexOf('<style'));
const TOPBAR_STYLE = TOPBAR.slice(TOPBAR.indexOf('<style'));

/** Properties that paint a color onto a control (shorthands included). */
const COLOR_PROP =
	/^(color|background(-color|-image)?|border(-(top|right|bottom|left))?(-color)?|outline(-color)?|box-shadow|fill|stroke|filter|opacity)$/;
/** Selector fragments that name the LYR trigger, its wrap, its menu or its shared button class. */
const LYR_SELECTOR = /lyr-wrap|topbar-slot-lyr(?!-compact)|lyr-menu|bsm-toggle|rb-topbar/;

/** Every .css and .svelte file under src, as [repo-relative path, text]. */
function styleSources(dir = SRC) {
	const out = [];
	for (const name of readdirSync(dir)) {
		const path = join(dir, name);
		if (statSync(path).isDirectory()) out.push(...styleSources(path));
		else if (/\.(css|svelte)$/.test(name))
			out.push([relative(FRONTEND, path), readFileSync(path, 'utf8')]);
	}
	return out;
}

/** Flat list of {selector, decls} rules from a stylesheet (comments stripped, @media bodies flattened). */
function rules(css) {
	const text = css.replace(/\/\*[\s\S]*?\*\//g, '');
	return [...text.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((m) => ({
		selector: m[1].trim().replace(/\s+/g, ' '),
		decls: [...m[2].matchAll(/([\w-]+)\s*:\s*([^;]+)/g)].map((d) => [d[1].trim(), d[2].trim()])
	}));
}

/** The opening tag of the element whose class attribute is `cls` (multi-line tags close on their own `>` line). */
function tagOf(src, cls) {
	const at = src.indexOf(`class="${cls}"`);
	assert.notEqual(at, -1, `class="${cls}" not found in TopBar markup`);
	const start = src.lastIndexOf('<', at);
	const close = src.slice(at).search(/\n\t*>/);
	assert.notEqual(close, -1, `no closing > for ${cls}`);
	return src.slice(start, at + close);
}

test('no Gothic-scoped rule anywhere in src targets the LYR trigger, its menu or the top bar', () => {
	// If a mono-dev selector names LYR, Gothic owns LYR's look and the skin-independent trigger contract breaks.
	const offenders = [];
	for (const [path, text] of styleSources()) {
		for (const { selector } of rules(text)) {
			if (selector.includes('mono-dev') && LYR_SELECTOR.test(selector))
				offenders.push(`${path}: ${selector}`);
		}
	}
	assert.deepEqual(offenders, []);
});

test('Gothic rules that reach every button change shape only, never LYR colors', () => {
	// The shared Gothic/Light shape rule hits every .perf-root button (LYR included); it may set radius, not paint.
	const theme = readFileSync(join(SRC, 'lib/rb/theme.css'), 'utf8');
	const generic = rules(theme).filter(
		({ selector }) => selector.includes('mono-dev') && /(^|[\s(,:])button\b|\[role=/.test(selector)
	);
	assert.ok(generic.length > 0, 'expected the Gothic shape rule for buttons in theme.css');
	for (const { selector, decls } of generic) {
		const painted = decls.filter(([prop]) => COLOR_PROP.test(prop));
		assert.deepEqual(painted, [], `${selector} paints buttons under Gothic`);
	}
});

test('LYR colors come only from the shared bsm-toggle tokens, which Gothic repaints by token', () => {
	// A literal color on LYR's own rules would ignore the Gothic token overrides and drift per skin.
	const own = rules(TOPBAR_STYLE).filter(({ selector }) =>
		/^\.bsm-toggle(\.on|:hover|:disabled)?$|topbar-slot-lyr(?!-compact)|lyr-wrap|lyr-menu/.test(
			selector
		)
	);
	assert.ok(
		own.some(({ selector }) => selector === '.bsm-toggle'),
		'base .bsm-toggle rule missing'
	);
	for (const { selector, decls } of own) {
		for (const [prop, value] of decls) {
			if (!COLOR_PROP.test(prop) || prop === 'opacity') continue;
			assert.doesNotMatch(
				value,
				/#[0-9a-f]{3,8}\b|\brgba?\(|\bhsla?\(/i,
				`${selector} { ${prop}: ${value} }`
			);
			assert.match(
				value,
				/var\(--rb-|^none$|^0$/,
				`${selector} { ${prop}: ${value} } is not a skin token`
			);
		}
	}
});

test('the LYR button is rendered for every skin inside its hover wrap, outside any conditional block', () => {
	// A skin-gated {#if} around LYR is how the trigger could stop resolving under Gothic.
	const at = MARKUP.indexOf('class="lyr-wrap topbar-slot-lyr-wrap"');
	assert.notEqual(at, -1);
	const before = MARKUP.slice(0, at);
	const opened = (before.match(/\{#if\b/g) ?? []).length;
	const closed = (before.match(/\{\/if\}/g) ?? []).length;
	assert.equal(opened, closed, 'LYR wrap sits inside an {#if} block');
	const wrap = tagOf(MARKUP, 'lyr-wrap topbar-slot-lyr-wrap');
	assert.match(wrap, /onpointerenter=\{_showLyrMenu\}/);
	const button = MARKUP.indexOf('class="bsm-toggle topbar-slot-lyr"');
	assert.ok(
		button > at && button < MARKUP.indexOf('</span>', at),
		'LYR button left its hover wrap'
	);
	assert.doesNotMatch(
		MARKUP,
		/ui_skin[^}]*\}[\s\S]{0,80}topbar-slot-lyr"/,
		'LYR markup keyed on ui_skin'
	);
});

test('the LYR button stays bound to the real lyrics action, not cosmetic markup', async () => {
	// The click sends set_lyrics_global with the flipped pref (LYR-14448); the command runs
	// setLyricsGlobal from the prefs store, and that setter must write and persist.
	const lyr = tagOf(MARKUP, 'bsm-toggle topbar-slot-lyr');
	assert.match(lyr, /^<button\b/);
	assert.match(lyr, /type="button"/);
	assert.match(
		lyr,
		/onclick=\{\(\) =>\s*void runPerformanceCommandFromUi\(\{ type: 'set_lyrics_global', enabled: !uiPrefs\.lyrics_global \}\)\}/
	);
	assert.match(lyr, /class:on=\{uiPrefs\.lyrics_global\}/);
	assert.match(lyr, /aria-pressed=\{uiPrefs\.lyrics_global\}/);
	const ipc = readFileSync(join(SRC, 'lib/rb/performance-ipc.svelte.ts'), 'utf8');
	assert.match(
		ipc,
		/command\.type === 'set_lyrics_global'\) \{\s*setLyricsGlobal\(command\.enabled\);/
	);

	const { makeLyricsPrefSetters } = await loadTypeScriptModule('src/lib/rb/lyrics-prefs.ts');
	const state = { lyrics_global: true };
	const synced = [];
	let persisted = 0;
	const setters = makeLyricsPrefSetters(
		state,
		() => (persisted += 1),
		(patch) => synced.push(patch)
	);
	setters.setLyricsGlobal(false);
	assert.equal(state.lyrics_global, false);
	setters.setLyricsGlobal(true);
	assert.equal(state.lyrics_global, true);
	assert.equal(persisted, 2);
	assert.deepEqual(synced, [{ lyrics_global: false }, { lyrics_global: true }]);
});

test('the LYR button is never disabled, even when the loaded track has no lyrics', () => {
	// LYR is the global overlay switch; only the Stage button inside its menu may disable.
	const lyr = tagOf(MARKUP, 'bsm-toggle topbar-slot-lyr');
	assert.doesNotMatch(lyr, /\sdisabled(?=[\s=>])/); // an attribute, not the selector node's `disabled: false` param
	assert.doesNotMatch(lyr, /aria-disabled/);
	assert.doesNotMatch(lyr, /\shidden\b/);
	assert.doesNotMatch(lyr, /pointer-events/);
	const stage = tagOf(MARKUP, 'bsm-toggle lyr-menu-stage');
	assert.match(
		stage,
		/disabled=\{stageTarget === null\}/,
		'Stage is the one LYR-menu control that disables'
	);
});
