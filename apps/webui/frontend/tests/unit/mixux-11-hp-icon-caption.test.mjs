/**
 * MIXUX-11 (the maintainer UI nit 3, Tue 6 Oct 2026): the headphone icon sits inside the
 * MIX and VOL captions ("[icon] MIX", "[icon] VOL"), not beside the dial.
 * The real HeadphoneCluster and Knob are rendered by svelte's SSR renderer
 * (see load-svelte-ssr.mjs for what that can and cannot prove).
 */
// requirement: MIXUX-11
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { FakeAudioContext, installWindow } from './fixtures/fake-web-audio.mjs';
import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const ENTRY = [
	"export { default as Panel } from '$lib/components/rb/mixer/HeadphoneCluster.svelte';",
	"export { default as Knob } from '$lib/components/rb/mixer/Knob.svelte';",
	"export { mixerState } from '$lib/player/state.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

let ssr;

before(async () => {
	installWindow('');
	globalThis.AudioContext = FakeAudioContext;
	ssr = await loadSvelteSsrModule(ENTRY);
});

after(() => {
	delete globalThis.AudioContext;
	delete globalThis.window;
});

function renderCluster() {
	const noop = () => {};
	const props = Object.fromEntries(
		['onmix', 'onlevel', 'ondelay', 'onrefresh', 'onacquire', 'onselect', 'onmaster', 'oninput', 'onmode', 'oncalibrate', 'onAlignmentMode'].map((name) => [name, noop])
	);
	return ssr.render(ssr.Panel, { props: { state: ssr.mixerState.headphones, ...props } }).body;
}

/** SSR hydration markers (`<!--[0-->`, `<!---->`) are not markup; drop them before matching. */
const stripMarkers = (html) => html.replace(/<!--[\s\S]*?-->/g, '');

/** The caption span of the knob whose data-testid is `knob-<id>`, from its open tag to `<label></span>`. */
function captionOf(html, id, label) {
	const start = html.indexOf(`data-testid="knob-${id}"`);
	assert.notEqual(start, -1, `knob ${id} must render`);
	const open = html.indexOf('<span class="label ', start);
	const close = html.indexOf(`${label}</span>`, open);
	assert.ok(open !== -1 && close !== -1, `knob ${id} must render a ${label} caption`);
	return html.slice(open, close + `${label}</span>`.length);
}

test('MIXUX-11: the MIX and VOL captions lead with the headphone icon', () => {
	const html = stripMarkers(renderCluster());
	for (const [id, label] of [
		['hp:hp-mix', 'MIX'],
		['hp:hp-level', 'VOL']
	]) {
		const caption = captionOf(html, id, label);
		assert.match(caption, /^<span class="label [^"]*"><span class="label-lead [^"]*"><svg class="hp-caption-icon/, `${label}: the icon must lead the caption`);
		assert.match(caption, new RegExp(`</svg></span>${label}</span>$`), `${label}: the word follows the icon`);
	}
	assert.doesNotMatch(html, /hp-control-icon/, 'the standalone icon beside the dial is gone');
	assert.equal(html.match(/hp-caption-icon/g)?.length, 2, 'exactly two headphone icons: MIX and VOL');
});

test('MIXUX-11 control: the aria-labels of MIX and VOL are unchanged', () => {
	const html = renderCluster();
	assert.match(html, /aria-label="Headphone CUE to MASTER mix"/);
	assert.match(html, /aria-label="Headphone cue gain"/);
});

test('MIXUX-11 control: a knob without captionLead renders a bare caption', () => {
	const html = stripMarkers(ssr.render(ssr.Knob, { props: { knobId: 'deck1:trim', label: 'TRIM', value: 0.5 } }).body);
	assert.match(html, /<span class="label[^"]*">TRIM<\/span>/);
	assert.doesNotMatch(html, /label-lead|<svg class="hp-caption-icon/);
});
