/**
 * Issue #13404 (spec e32be81d_hot-cue-ax-names.md section 3): each hot-cue pad's
 * accessible name is exactly `hot cue <slot> deck <deck>`. Commit 870fbcc4 once
 * appended the proposal caption to the pad's aria-label, so a pad with a visible
 * proposal answered to `hot cue A deck 1 proposed intro` and an AX-tree agent
 * could no longer address it. These tests render the real HotCueBank (svelte
 * SSR) and read the name off the rendered pad button, so they pin the runtime
 * name rather than a source string.
 */
import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const ENTRY = [
	"export { default as HotCueBank } from '$lib/components/rb/deck/HotCueBank.svelte';",
	"export { deckStates } from '$lib/rb/audio-engine.svelte';",
	"export { ensureAutoCues, getAutoCuesEntry } from '$lib/components/rb/deck/auto-cues-cache.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

/** @type {Awaited<ReturnType<typeof loadSvelteSsrModule>>} */
let mod;
let originalFetch;

before(async () => {
	mod = await loadSvelteSsrModule(ENTRY);
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

beforeEach(() => {
	for (const id of [1, 2]) {
		const deck = mod.deckStates[id];
		deck.stable_id = null;
		deck.hot_cues = [];
	}
});

function cue(slot, comment) {
	return {
		slot,
		in_ms: 1000,
		out_ms: null,
		is_loop: false,
		beat_loop_size: null,
		color_table_index: null,
		comment
	};
}

function renderBank(deckId) {
	return mod.render(mod.HotCueBank, {
		props: {
			deck: mod.deckStates[deckId],
			pending: false,
			onJump: async () => {},
			onSave: async () => ({}),
			onRename: async () => ({}),
			onDelete: async () => ({}),
			onRestore: async () => {}
		}
	}).body;
}

/** The opening tag of the pad button for one deck and slot. */
function padTag(html, deckId, slot) {
	const tags = html.match(/<button\b[^>]*>/g) ?? [];
	const found = tags.filter((tag) => tag.includes(`data-testid="hot-cue-${deckId}-${slot}"`));
	assert.equal(found.length, 1, `exactly one pad button for deck ${deckId} slot ${slot}`);
	return found[0];
}

function ariaLabel(tag) {
	const m = tag.match(/\saria-label="([^"]*)"/);
	assert.ok(m, `pad has no aria-label: ${tag}`);
	return m[1];
}

test('[if] deck one renders hot cue A [then] the pad is named "hot cue A deck 1"', () => {
	// Deck one's pad A carries the deck-scoped name and test id.
	mod.deckStates[1].stable_id = 'ax-fixture-1';
	mod.deckStates[1].hot_cues = [cue('A', null)];
	const tag = padTag(renderBank(1), 1, 'A');
	assert.equal(ariaLabel(tag), 'hot cue A deck 1');
});

test('[if] deck two renders the same cue slot [then] its pad name carries deck 2, not deck 1', () => {
	// The same slot on two decks yields two distinct names, one per deck.
	mod.deckStates[1].stable_id = 'ax-fixture-1';
	mod.deckStates[1].hot_cues = [cue('A', null)];
	mod.deckStates[2].stable_id = 'ax-fixture-2';
	mod.deckStates[2].hot_cues = [cue('A', null)];
	const one = renderBank(1);
	const two = renderBank(2);
	assert.equal(ariaLabel(padTag(two, 2, 'A')), 'hot cue A deck 2');
	assert.equal(ariaLabel(padTag(one, 1, 'A')), 'hot cue A deck 1');
	assert.doesNotMatch(two, /data-testid="hot-cue-1-A"/, 'deck two must not render deck one pads');
	// Empty slots are named the same way.
	assert.equal(ariaLabel(padTag(two, 2, 'B')), 'hot cue B deck 2');
});

test('[if] a cue has a custom caption [then] the caption is shown but not folded into the pad name', () => {
	// A user comment on the cue stays visible text and never enters the AX name.
	mod.deckStates[1].stable_id = 'ax-fixture-1';
	mod.deckStates[1].hot_cues = [cue('A', 'Big Drop')];
	const html = renderBank(1);
	const tag = padTag(html, 1, 'A');
	assert.equal(ariaLabel(tag), 'hot cue A deck 1');
	assert.match(
		html,
		/<span class="cue-label[^"]*"[^>]*>Big Drop<\/span>/,
		'the custom caption still renders'
	);
});

test('[if] an empty slot shows a proposal caption [then] the caption is not appended to the pad name', async () => {
	// The 870fbcc4 regression: a visible proposal caption must not leak into aria-label.
	const sid = 'ax-fixture-proposal';
	globalThis.fetch = async () =>
		new Response(
			JSON.stringify({
				stable_id: sid,
				proposal: true,
				source: 'apps.analysis',
				backend: 'librosa',
				backend_version: '1.0',
				proposals: [
					{ time_s: 12.4, kind: 'intro', confidence: 0.9, source: 'heuristic', rms_dbfs: -12 }
				]
			}),
			{ status: 200, headers: { 'content-type': 'application/json' } }
		);
	mod.ensureAutoCues(sid);
	for (let i = 0; i < 50 && mod.getAutoCuesEntry(sid)?.status !== 'ready'; i += 1) {
		await new Promise((resolve) => setTimeout(resolve, 0));
	}
	assert.equal(mod.getAutoCuesEntry(sid)?.status, 'ready', 'fixture proposals must load');
	mod.deckStates[1].stable_id = sid;
	const html = renderBank(1);
	const tag = padTag(html, 1, 'A');
	assert.match(tag, /data-proposal-kind="intro"/, 'the proposal is visible on pad A');
	assert.equal(ariaLabel(tag), 'hot cue A deck 1');
	assert.doesNotMatch(ariaLabel(tag), /proposed/);
});

test('[if] the source reintroduces a second name attribute [then] the pad fails this check', () => {
	// The pad is named by aria-label alone: no aria-labelledby or aria-description beside it.
	mod.deckStates[1].stable_id = 'ax-fixture-1';
	mod.deckStates[1].hot_cues = [cue('A', 'Big Drop')];
	const html = renderBank(1);
	for (const slot of ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H']) {
		const tag = padTag(html, 1, slot);
		assert.equal(
			(tag.match(/\saria-label=/g) ?? []).length,
			1,
			`pad ${slot} has exactly one aria-label`
		);
		assert.doesNotMatch(tag, /\saria-labelledby=/, `pad ${slot} must not gain aria-labelledby`);
		assert.doesNotMatch(tag, /\saria-description=/, `pad ${slot} must not gain aria-description`);
		assert.doesNotMatch(tag, /\saria-describedby=/, `pad ${slot} must not gain aria-describedby`);
	}
});
