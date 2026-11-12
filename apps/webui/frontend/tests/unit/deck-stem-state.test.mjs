// Issue #12427: the deck root mirrors stem row normalized state on data-stems-state.
//
// [if] a deck has no stem bundle [then] the deck root reads none [else stop]
// [if] a stem load fails [then] the deck root reads error [else stop]
// [if] a stem load is fetching [then] the deck root reads fetching [else stop]
// [if] stems are ready [then] the deck root reads ready [else stop]
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { before, describe, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const FAKE_NAVIGATION = fileURLToPath(new URL('./fake-app-navigation.mjs', import.meta.url));

const ENTRY = [
	"export { default as Deck } from '$lib/components/rb/Deck.svelte';",
	"export { deckStates } from '$lib/rb/audio-engine.svelte';",
	"export { loadingStemDeckState, readyStemDeckState, unavailableStemDeckState } from '$lib/rb/stem-graph';",
	"export { render } from 'svelte/server';"
].join('\n');

let mod;

before(async () => {
	globalThis.requestAnimationFrame = () => 0;
	globalThis.cancelAnimationFrame = () => {};
	mod = await loadSvelteSsrModule(ENTRY, { alias: { '$app/navigation': FAKE_NAVIGATION } });
});

function decode(text) {
	return text
		.replaceAll('&quot;', '"')
		.replaceAll('&#39;', "'")
		.replaceAll('&lt;', '<')
		.replaceAll('&gt;', '>')
		.replaceAll('&amp;', '&');
}

function attr(tag, name) {
	const match = tag.match(new RegExp(`\\s${name}="([^"]*)"`));
	return match === null ? null : decode(match[1]);
}

const ALIGNMENT = { sample_rate_hz: 44100, frame_count: 100, channel_count: 2, duration_ms: 2 };
const DECK_ID = 1;

function deckRootStemState(stems) {
	mod.deckStates[DECK_ID].stems = stems;
	const html = mod.render(mod.Deck, { props: { deckId: DECK_ID } }).body;
	const root = html.match(/<section class="rb-deck[^"]*"[^>]*>/);
	assert.ok(root, `Deck rendered no .rb-deck section:\n${html}`);
	return attr(root[0], 'data-stems-state');
}

describe('Deck root data-stems-state (issue #12427)', () => {
	test('no stem bundle: none', () => {
		assert.equal(
			deckRootStemState(mod.unavailableStemDeckState('no stem bundle for t-42')),
			'none'
		);
	});

	test('stem load failed: error', () => {
		assert.equal(
			deckRootStemState({
				...mod.unavailableStemDeckState('decode failed: bad frame'),
				status: 'error'
			}),
			'error'
		);
	});

	test('stem load fetching: fetching', () => {
		assert.equal(
			deckRootStemState(
				mod.loadingStemDeckState({ phase: 'fetching', progress: null, reason: null })
			),
			'fetching'
		);
	});

	test('stems ready: ready', () => {
		assert.equal(
			deckRootStemState(
				mod.readyStemDeckState(
					{ source: 'demucs', model: 'htdemucs', layout: 'demucs4' },
					ALIGNMENT
				)
			),
			'ready'
		);
	});

	test('stems switched off: off', () => {
		assert.equal(
			deckRootStemState(mod.unavailableStemDeckState('stems disabled: less mode')),
			'off'
		);
	});

	test('empty deck: empty', () => {
		assert.equal(deckRootStemState(mod.unavailableStemDeckState(null)), 'empty');
	});
});
