// requirement: PGMIX-7692d
// Comment text stays readable in light mode (Preview f027565ab5, pin 18ba998a9836).
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const read = (rel) => readFileSync(`src/${rel}`, 'utf8');
const APP_CSS = read('app.css');
const LAYOUT = read('routes/+layout.svelte');

function styleOf(src) {
	const start = src.indexOf('<style');
	assert.ok(start !== -1, 'component has a <style> block');
	// Comments may name the old tokens; only declarations count.
	return src.slice(start).replace(/\/\*[\s\S]*?\*\//g, '');
}

test('the pin layer mounts on the root layout, outside .perf-root', () => {
	assert.match(LAYOUT, /<FeedbackPinLayer \/>/);
	assert.doesNotMatch(LAYOUT, /perf-root/);
});

test('the shell tokens the pins read are defined for dark, light theme and Light skin', () => {
	for (const sel of [':root', "html[data-theme='light']", "html[data-skin='light']"]) {
		const start = APP_CSS.indexOf(`${sel} {`);
		assert.ok(start !== -1, `${sel} block`);
		const body = APP_CSS.slice(start, APP_CSS.indexOf('\n}', start));
		for (const token of ['--fg', '--bg', '--surface', '--border']) {
			assert.match(body, new RegExp(`${token}:`), `${sel} defines ${token}`);
		}
	}
});

/** The CSS rule (selector through closing brace) that contains `marker`. */
function ruleWith(css, marker) {
	const at = css.indexOf(marker);
	assert.ok(at !== -1, `a rule containing ${marker}`);
	const open = css.lastIndexOf('{', at);
	const selStart = css.lastIndexOf('}', open) + 1;
	return css.slice(selStart, css.indexOf('}', at) + 1);
}

// The comment surface (the 320px scroller) and the typing box of each pin.
const SURFACES = {
	'lib/components/rb/FeedbackPinCard.svelte': ['max-height: 320px;', 'resize: vertical;'],
	'lib/components/rb/FeedbackPinDraftBubble.svelte': ['max-height: 320px;', 'caret-color']
};

for (const [file, markers] of Object.entries(SURFACES)) {
	const css = styleOf(read(file));
	for (const marker of markers) {
		test(`${file}: the rule with "${marker}" paints from the shell pair`, () => {
			const rule = ruleWith(css, marker);
			// The --rb-* color palette is defined only under .perf-root, so it never resolves here.
			assert.doesNotMatch(rule, /var\(--rb-(text|well|inset|border|panel)/, rule);
			assert.match(rule, /background: var\(--(surface|bg), #[0-9a-f]{6}\);/);
			assert.match(rule, /color: var\(--fg, #e6e9ef\);/);
		});
	}
	test(`${file}: typed text and caret use --fg`, () => {
		assert.match(css, /-webkit-text-fill-color: var\(--fg, #e6e9ef\);/);
		assert.match(css, /caret-color: var\(--fg, #e6e9ef\);/);
	});
}
