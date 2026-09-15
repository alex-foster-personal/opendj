// requirement: CUEOUT-13 (two-outputs headphone row stays inside the mixer column)
// [if] two outputs adds pinned sinks, HEAD DELAY and the drift warning [then] the headphone row wraps instead of widening the mixer
// [if] a descendant is focused or scrolled into view [then] the mixer panel cannot scroll sideways and hide channel strips
//
// Regression line: if `.hp` does not wrap or `.rb-mixer` can scroll then selecting a MASTER / CUE device
// (which switches to two outputs) grows the row to ~501px inside a ~234px column, the overflow-hidden
// mixer scrolls ~82px, and channel 1 is cut off while channel 3 and the lower mixer vanish
// (observed live Tue 15 Sep 2026 after picking LG ULTRAWIDE as master).
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const FRONTEND = new URL('../../', import.meta.url);

function styleBlock(relativePath) {
	const source = readFileSync(new URL(relativePath, FRONTEND), 'utf8');
	const match = source.match(/<style>([\s\S]*?)<\/style>/);
	assert.ok(match, `${relativePath} must have a <style> block`);
	return match[1];
}

function ruleBody(css, selector) {
	const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
	const match = css.match(new RegExp(`(?:^|\\n)\\s*${escaped}\\s*\\{([^}]*)\\}`));
	assert.ok(match, `rule ${selector} must exist`);
	return match[1];
}

test('if the headphone row cannot wrap then two outputs widens the mixer past its column', () => {
	const css = styleBlock('src/lib/components/rb/mixer/HeadphoneCluster.svelte');
	const hp = ruleBody(css, '.hp');
	assert.match(hp, /flex-wrap:\s*wrap/, '.hp must wrap its controls');
	assert.match(hp, /min-width:\s*0/, '.hp must be allowed to shrink below its content width');
});

test('if the mixer row or panel can grow or scroll sideways then channel strips get hidden', () => {
	const css = styleBlock('src/lib/components/rb/Mixer.svelte');
	assert.match(ruleBody(css, '.hp-row'), /min-width:\s*0/, '.hp-row must be allowed to shrink');
	assert.match(
		ruleBody(css, '.rb-mixer'),
		/overflow:\s*clip/,
		'.rb-mixer must clip (not hidden) so focus or scrollIntoView cannot scroll it sideways'
	);
});
