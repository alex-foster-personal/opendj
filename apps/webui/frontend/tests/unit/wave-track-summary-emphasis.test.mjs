import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const summaryPath = path.join(
	__dirname,
	'../../src/lib/components/rb/wave/WaveTrackSummary.svelte'
);
const themePath = path.join(__dirname, '../../src/lib/rb/theme.css');
const waveRowPath = path.join(__dirname, '../../src/lib/components/rb/wave/WaveRow.svelte');

// pin e585d3b67f4d: "the title below it is too loud (full white font) and too
// wide. Empty and no track loaded for state there is too loud".
//
// These tests RESOLVE THE CASCADE rather than grepping the file. A blinded
// reviewer of PR #1681 showed why: a test that slices out `.wave-track-name {`
// and reads only that rule still passes when a later, MORE SPECIFIC selector
// (`.wave-track-summary .wave-track-name { color: var(--rb-text) }`, 0-2-0
// against 0-1-0) wins in the browser and puts the title back to full
// brightness. That is the exact regression the pin was filed for, so the guard
// has to answer "what actually wins", not "does this substring still exist".

//----- cascade ---------------------------------------------------------------

/** Cut every at-rule and its whole (possibly nested) body out of `css`. */
function _dropAtRuleBlocks(css) {
	let out = '';
	let index = 0;
	while (index < css.length) {
		const at = css.indexOf('@', index);
		if (at === -1) return out + css.slice(index);
		out += css.slice(index, at);
		const open = css.indexOf('{', at);
		const semicolon = css.indexOf(';', at);
		if (open === -1 || (semicolon !== -1 && semicolon < open)) {
			// A statement at-rule (@import, @charset): no body to skip.
			index = semicolon === -1 ? css.length : semicolon + 1;
			continue;
		}
		let depth = 0;
		let cursor = open;
		for (; cursor < css.length; cursor += 1) {
			if (css[cursor] === '{') depth += 1;
			else if (css[cursor] === '}' && (depth -= 1) === 0) break;
		}
		index = cursor + 1;
	}
	return out;
}

/** Every declaration block in the component's scoped <style>, in source order. */
function styleRules(source) {
	const open = source.indexOf('<style>');
	const close = source.lastIndexOf('</style>');
	assert.ok(open !== -1 && close > open, 'the component must have a <style> block');
	const withComments = source.slice(open + '<style>'.length, close);
	// Nested at-rule bodies (@keyframes here) are cut out whole, braces and
	// all, before the flat scan below - left in, their inner blocks would be
	// read as ordinary rules and their declarations attributed to the wrong
	// selector. A conditional at-rule (@media, @container, @supports) DOES
	// hold real rules and cannot simply be dropped, so this refuses instead.
	const css = _dropAtRuleBlocks(withComments.replace(/\/\*[\s\S]*?\*\//g, ''));
	assert.ok(
		!/@[a-z-]+/.test(css),
		'this resolver drops @keyframes-style blocks only; teach it conditional at-rules first'
	);
	const rules = [];
	for (const match of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
		for (const selector of match[1].split(',')) {
			rules.push({ selector: selector.trim(), declarations: match[2], order: rules.length });
		}
	}
	assert.ok(rules.length > 0, 'no rules parsed out of the style block');
	return rules;
}

/** CSS specificity as a sortable number: ids, then classes, then elements. */
function specificity(selector) {
	const subject = selector.split(/[\s>+~]+/).filter(Boolean).pop() ?? '';
	const ids = (selector.match(/#[\w-]+/g) ?? []).length;
	const classes = (selector.match(/\.[\w-]+|\[[^\]]+\]|:(?!:)[\w-]+/g) ?? []).length;
	const elements = (selector.match(/(^|[\s>+~])[a-z][\w-]*/g) ?? []).length;
	return { score: ids * 10000 + classes * 100 + elements, subject };
}

/**
 * The value that actually wins for `property` on an element carrying
 * `classes`. Ancestor parts of a selector are treated as satisfiable, which is
 * the conservative direction: a rule that MIGHT apply is counted, so a
 * competing override can never be missed.
 */
function winningValue(rules, classes, property) {
	let best = null;
	for (const rule of rules) {
		const { score, subject } = specificity(rule.selector);
		const subjectClasses = (subject.match(/\.[\w-]+/g) ?? []).map((c) => c.slice(1));
		if (subjectClasses.length === 0) continue;
		if (!subjectClasses.every((c) => classes.includes(c))) continue;
		const declared = rule.declarations.match(
			new RegExp(`(?:^|;)\\s*${property}\\s*:\\s*([^;]+)`, 'i')
		);
		if (declared === null) continue;
		if (best === null || score > best.score || (score === best.score && rule.order > best.order)) {
			best = { score, order: rule.order, value: declared[1].trim(), selector: rule.selector };
		}
	}
	return best;
}

//----- contrast --------------------------------------------------------------

function relativeLuminance(hex) {
	const channels = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
	const [r, g, b] = channels.map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
	return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrastRatio(fg, bg) {
	const [light, dark] = [relativeLuminance(fg), relativeLuminance(bg)].sort((a, b) => b - a);
	return (light + 0.05) / (dark + 0.05);
}

/** Token -> hex, read out of one selector block of the real theme file. */
function paletteFor(themeCss, selector) {
	const start = themeCss.indexOf(selector);
	assert.ok(start !== -1, `theme.css must still define ${selector}`);
	const block = themeCss.slice(start, themeCss.indexOf('}', start));
	const palette = {};
	for (const match of block.matchAll(/(--rb-[\w-]+):\s*(#[0-9a-fA-F]{6})/g)) {
		palette[match[1]] = match[2];
	}
	return palette;
}

//----- the guards ------------------------------------------------------------

test('the winning colour for the waveform track name is the dim token, not the primary one', () => {
	const rules = styleRules(readFileSync(summaryPath, 'utf8'));
	const won = winningValue(rules, ['wave-track-name'], 'color');
	assert.ok(won !== null, 'some rule must set the track name colour');
	assert.equal(
		won.value,
		'var(--rb-text-dim)',
		`the cascade winner is "${won.selector} { color: ${won.value} }" - a title at primary ` +
			'weight is exactly what pin e585d3b67f4d called too loud'
	);
});

test('the dim token clears the 4.5:1 AA floor on every surface this title renders on', () => {
	const themeCss = readFileSync(themePath, 'utf8');
	// The one background in play here that is NOT a token: decks 3/4 paint
	// their whole row, gutter included, with a literal hex in WaveRow.svelte.
	const secondaryRow = readFileSync(waveRowPath, 'utf8').match(
		/\.rb-waverow\.secondary\s*\{[^}]*background:\s*(#[0-9a-fA-F]{6})/
	);
	assert.ok(secondaryRow !== null, 'WaveRow must still declare the secondary-row fill');

	const dark = paletteFor(themeCss, '.perf-root {');
	const light = paletteFor(themeCss, "html[data-theme='light'] .perf-root {");
	const surfaces = [
		['dark --rb-bg', dark['--rb-text-dim'], dark['--rb-bg']],
		['dark --rb-panel', dark['--rb-text-dim'], dark['--rb-panel']],
		['dark --rb-panel-raised', dark['--rb-text-dim'], dark['--rb-panel-raised']],
		['dark deck 3/4 row', dark['--rb-text-dim'], secondaryRow[1]],
		['light --rb-bg', light['--rb-text-dim'], light['--rb-bg']],
		['light --rb-panel', light['--rb-text-dim'], light['--rb-panel']],
		['light --rb-panel-raised', light['--rb-text-dim'], light['--rb-panel-raised']]
		// DELIBERATELY NOT LISTED: light theme on the deck 3/4 row. That row's
		// fill is a hardcoded dark hex with no light-theme override, so this
		// title measures 2.31:1 there - a real, PRE-EXISTING AA failure that
		// belongs to WaveRow's palette, not to this title's colour. Listing it
		// would make this test red for a defect it cannot fix; omitting it
		// silently would hide it. It is tracked as issue #1722, and the
		// number is recorded here so nobody has to rediscover it: before the
		// dim token this same cell measured 1.07:1, so this change improved it
		// 2.2x without clearing the floor.
	];
	for (const [name, fg, bg] of surfaces) {
		assert.ok(fg !== undefined && bg !== undefined, `missing colours for ${name}`);
		const ratio = contrastRatio(fg, bg);
		assert.ok(
			ratio >= 4.5,
			`${name}: ${fg} on ${bg} is ${ratio.toFixed(2)}:1, under the 4.5:1 AA body floor`
		);
	}
});

test('the winning width for the waveform track name shrinks it to its own content', () => {
	const rules = styleRules(readFileSync(summaryPath, 'utf8'));
	for (const [property, expected] of [
		['width', 'fit-content'],
		['max-width', '100%'],
		['overflow', 'hidden']
	]) {
		const won = winningValue(rules, ['wave-track-name'], property);
		assert.ok(won !== null, `some rule must set the track name ${property}`);
		assert.equal(
			won.value,
			expected,
			`the cascade winner is "${won.selector} { ${property}: ${won.value} }"; a title at ` +
				'width 100% fills the whole gutter and the hover scrub means nothing'
		);
	}
});

test('the no-track and no-artwork slates give up the raised fill', () => {
	const source = readFileSync(summaryPath, 'utf8');
	// By class TOKEN, not by a byte-exact attribute string: reordering classes
	// or attributes renders identically, so it must not fail this guard.
	for (const title of ['No track loaded', 'Artwork unavailable']) {
		const tag = source.match(new RegExp(`<span[^>]*title="${title}[^"]*"[^>]*>`));
		assert.ok(tag !== null, `the ${title} slate must still exist`);
		const classes = (tag[0].match(/class="([^"]*)"/)?.[1] ?? '').split(/\s+/);
		for (const required of ['wave-art-slate', 'visible', 'standalone']) {
			assert.ok(classes.includes(required), `the ${title} slate must carry .${required}`);
		}
	}
	const rules = styleRules(source);
	const background = winningValue(rules, ['wave-art-slate', 'visible', 'standalone'], 'background');
	assert.ok(background !== null, 'some rule must set the standalone slate background');
	assert.equal(
		background.value,
		'transparent',
		`the cascade winner is "${background.selector} { background: ${background.value} }"; a ` +
			'standalone slate painting the raised chrome fill is the loud empty state the pin names'
	);
	const emptyColour = winningValue(rules, ['wave-track-name', 'empty'], 'font-style');
	assert.ok(emptyColour !== null, 'the empty-state name needs its own quieter treatment');
});
