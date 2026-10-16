// UICONTRAST-02: the footer MIDI buttons and SET OUTPUTS are quiet by color,
// never by opacity (alpha status B25: opacity drew them at 2.1-2.8:1).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const src = (p) => readFileSync(new URL(`../../src/lib/components/rb/${p}`, import.meta.url), 'utf8');

function ruleBody(css, selector) {
	const re = new RegExp(`(^|\\n)\\s*${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*\\{([^}]*)\\}`);
	const m = css.match(re);
	assert.ok(m, `rule ${selector} not found`);
	return m[2];
}

test('tray MIDI label and its hover carry no opacity', () => {
	const css = src('browser-panel-tray.css');
	for (const sel of ['.tray-midi', '.tray-midi:hover']) {
		const body = ruleBody(css, sel);
		assert.doesNotMatch(body, /opacity/, `${sel} dims by opacity`);
	}
	assert.match(ruleBody(css, '.tray-midi'), /color:\s*var\(--rb-text-dim\)/);
});

test('idle headphone MIDI button is dimmed by color, not opacity', () => {
	const body = ruleBody(src('mixer/HeadphoneCluster.svelte'), '.midi-btn.st-grey');
	assert.doesNotMatch(body, /opacity/);
	assert.match(body, /color:\s*var\(--rb-text-dim/);
});

test('SET OUTPUTS lifts its red on dark panels and keeps the raw red only on light surfaces', () => {
	const css = src('mixer/HeadphoneCluster.svelte');
	assert.match(ruleBody(css, '.hp-btn-io.io-alert'), /color:\s*color-mix\(in srgb, var\(--rb-red[^)]*\) 70%, #fff\)/);
	const light = css.match(/:global\(html:is\(([^{]*)\)\)\s*\.hp-btn-io\.io-alert\s*\{([^}]*)\}/);
	assert.ok(light, 'light-surface override for SET OUTPUTS not found');
	assert.match(light[1], /\[data-skin='light'\]/);
	// Gothic (mono-dev) stays dark under the light theme, so the theme half must exclude any skin.
	assert.match(light[1], /\[data-theme='light'\]:not\(\[data-skin\]\)/);
	assert.match(light[2], /color:\s*var\(--rb-red/);
});
