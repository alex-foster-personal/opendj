/**
 * GRID-OCTAVE-02: the tempo-dispute marker, its hover line and the one-click
 * "use the other tempo", RENDERED with svelte's server renderer (no DOM in
 * this suite, so the click itself is exercised through the same API call the
 * card's button makes).
 *
 * Regression lines:
 * - if the hover line does not name both tempos, the confidence and which one is shown then broken
 * - if the marker is not an inline SVG (an emoji or a text symbol instead) then broken
 * - if the card offers the tempo already shown instead of the other one then broken
 * - if picking the other tempo does not PUT {"source"} to /bpm-pick then broken
 * - if a refused pick does not throw (so the card and toast can show it) then broken
 * - if a USB-stick track's pick reaches the library route instead of being refused first then broken (GRID-OCTAVE-03)
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const ENTRY = [
	"export { default as Marker } from '$lib/components/rb/BpmDisputeMarker.svelte';",
	"export { default as Card } from '$lib/components/rb/BpmDisputeCard.svelte';",
	"export { bpmDisputeText, otherAlternative, useAlternativeLabel } from '$lib/rb/bpm-dispute';",
	"export { putBpmPick } from '$lib/rb/api-rb';",
	"export { render } from 'svelte/server';"
].join('\n');

const GREY = [
	{ source: 'own', bpm: 129.59 },
	{ source: 'mik', bpm: 93.75 }
];
const EMOJI = /\p{Extended_Pictographic}/u;

let mod;

before(async () => {
	mod = await loadSvelteSsrModule(ENTRY);
});

test('the hover line names both tempos, the confidence and the shown one', () => {
	assert.equal(
		mod.bpmDisputeText(GREY, 129.59, 0.0),
		'Mixed In Key: 93.75 BPM, our analysis: 129.59 BPM (confidence 0.0). Showing ours.'
	);
	assert.equal(
		mod.bpmDisputeText(GREY, 93.75, 0.42),
		"Mixed In Key: 93.75 BPM, our analysis: 129.59 BPM (confidence 0.4). Showing Mixed In Key's."
	);
	assert.match(mod.bpmDisputeText(GREY, 129.59, null), /confidence unknown/);
	assert.equal(mod.bpmDisputeText([], 128, 0.9), null, 'agreeing tempos carry no line');
});

test('the marker is an inline SVG glyph, never an emoji or text symbol', () => {
	const html = mod.render(mod.Marker).body;
	assert.match(html, /<svg[^>]*class="bpm-dispute-glyph/);
	assert.match(html, /<path /);
	assert.equal(EMOJI.test(html), false, `emoji in marker: ${html}`);
});

test('the card shows the line and offers the OTHER tempo', () => {
	const props = {
		alternatives: GREY,
		shownBpm: 129.59,
		confidence: 0.0,
		anchor: { left: 0, top: 0, width: 10, height: 10 },
		onpick: async () => {},
		onerror: () => {},
		onenter: () => {},
		onleave: () => {},
		onDismiss: () => {}
	};
	const html = mod.render(mod.Card, { props }).body;
	assert.match(html, /Mixed In Key: 93\.75 BPM, our analysis: 129\.59 BPM \(confidence 0\.0\)\. Showing ours\./);
	assert.match(html, /data-testid="bpm-dispute-use-other"[^>]*>\s*Use Mixed In Key's 93\.75 BPM/);
	assert.match(html, /data-hover-card/);
	assert.deepEqual(mod.otherAlternative(GREY, 93.75), { source: 'own', bpm: 129.59 });
	const none = mod.render(mod.Card, { props: { ...props, alternatives: [] } }).body;
	assert.equal(none.includes('bpm-dispute-card'), false, 'no dispute, no card');
});

test('picking the other tempo PUTs its source, and a refusal throws', async () => {
	const calls = [];
	const realFetch = globalThis.fetch;
	globalThis.fetch = async (url, init) => {
		calls.push({ url: String(url), method: init.method, body: init.body });
		if (calls.length === 2) {
			return new Response(JSON.stringify({ detail: { code: 'TRACK_NOT_FOUND', message: 'no track x' } }), { status: 404 });
		}
		return new Response(JSON.stringify({ stable_id: 'abc', source: 'mik' }), { status: 200 });
	};
	try {
		const other = mod.otherAlternative(GREY, 129.59);
		assert.deepEqual(await mod.putBpmPick('abc', other.source), { stable_id: 'abc', source: 'mik' });
		await assert.rejects(mod.putBpmPick('x', 'own'), /no track x/);
	} finally {
		globalThis.fetch = realFetch;
	}
	assert.match(calls[0].url, /\/api\/v1\/tracks\/abc\/bpm-pick$/);
	assert.equal(calls[0].method, 'PUT');
	assert.deepEqual(JSON.parse(calls[0].body), { source: 'mik' });
});

test('a USB-stick track pick is refused before any request (GRID-OCTAVE-03)', async () => {
	const realFetch = globalThis.fetch;
	let fetched = 0;
	globalThis.fetch = async () => {
		fetched += 1;
		return new Response('{}', { status: 200 });
	};
	try {
		await assert.rejects(mod.putBpmPick('usb-stick-1', 'mik'), /USB_READ_ONLY|read-only|USB/i);
	} finally {
		globalThis.fetch = realFetch;
	}
	assert.equal(fetched, 0, 'if a stick id reaches /api/v1/tracks/{id}/bpm-pick then the library is asked about a track it does not own: broken');
});
