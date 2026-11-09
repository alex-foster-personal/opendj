/**
 * Issue #10303: the KEY SYNC ladder chevron stays inside the KEY SYNC button
 * box so a narrow 4-deck column cannot clip it.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';
import { compile } from 'svelte/compiler';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const DECK_HEADER_PATH = fileURLToPath(
	new URL('../../src/lib/components/rb/deck/DeckHeader.svelte', import.meta.url)
);

const ENTRY = [
	"export { default as DeckHeader } from '$lib/components/rb/deck/DeckHeader.svelte';",
	"export { _emptyDeckState } from '$lib/player/state.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

/** @type {Awaited<ReturnType<typeof loadSvelteSsrModule>>} */
let mod;

before(async () => {
	mod = await loadSvelteSsrModule(ENTRY);
});

function renderDeckHeader(deckId = 1) {
	const deck = mod._emptyDeckState(deckId);
	deck.stable_id = 'fixture-stable';
	deck.title = 'Fixture track';
	deck.artist = 'Fixture artist';
	deck.key = '8A';
	return mod.render(mod.DeckHeader, {
		props: {
			deck,
			deckId,
			pending: false,
			onBeatSync: async () => {},
			onMaster: async () => {},
			onMasterTempo: async () => {},
			onKeySync: async () => {},
			onKeyNudge: async () => {},
			onResetTempo: async () => {},
			onUnload: async () => {},
			keySyncAvailable: true,
			mirrored: false
		}
	}).body;
}

/** Slice the outer element whose class list contains `classToken`. */
function firstElementSlice(html, classToken) {
	const needle = `class="${classToken}`;
	const open = html.indexOf(needle);
	assert.notEqual(open, -1, `expected .${classToken} in SSR body`);
	const tagStart = html.lastIndexOf('<', open);
	const tagEnd = html.indexOf('>', open);
	assert.notEqual(tagStart, -1, `malformed .${classToken} open tag`);
	assert.notEqual(tagEnd, -1, `malformed .${classToken} open tag`);
	const tagName = html.slice(tagStart + 1, tagEnd).split(/\s/)[0];
	const close = `</${tagName}>`;
	const closeIdx = html.indexOf(close, tagEnd);
	assert.notEqual(closeIdx, -1, `.${classToken} never closed`);
	return html.slice(tagStart, closeIdx + close.length);
}

function assertNonNegativeInset(css, ruleSelector) {
	const rule = css.match(new RegExp(`${ruleSelector.replace('.', '\\.')}[^{]*\\{([^}]+)\\}`));
	assert.ok(rule, `missing compiled rule ${ruleSelector}`);
	const body = rule[1];
	const right = body.match(/right:\s*(-?\d+(?:\.\d+)?)px/);
	assert.ok(right, `${ruleSelector} must pin a right inset`);
	assert.ok(Number(right[1]) >= 0, `${ruleSelector} must not use a negative right offset`);
	assert.doesNotMatch(body, /margin[^;]*-\d/, `${ruleSelector} must not use a negative margin`);
}

test('[if] DeckHeader renders with a loaded track [then] the ladder chevron sits in the KEY SYNC button box', () => {
	const html = renderDeckHeader(1);
	const keySyncIdx = html.indexOf('data-testid="key-sync-deck-1"');
	assert.notEqual(keySyncIdx, -1, 'KEY SYNC button must render');
	const chevronIdx = html.indexOf('data-testid="key-ladder-open-deck-1"');
	assert.notEqual(chevronIdx, -1, 'KEY SYNC ladder chevron must render');

	const keyPair = firstElementSlice(html, 'key-pair');
	const keySync = firstElementSlice(keyPair, 'rb-lit-button keysync');
	assert.match(keySync, />KEY SYNC</, 'KEY SYNC label must render');
	const chevron = firstElementSlice(keyPair, 'keysync-chevron');
	assert.ok(
		keyPair.includes(keySync) && keyPair.includes(chevron),
		'the chevron and KEY SYNC control must share the key-pair button box'
	);
	assert.ok(
		keyPair.indexOf(keySync) < keyPair.indexOf(chevron),
		'the ladder chevron must follow the KEY SYNC button inside the same box'
	);
});

test('[if] DeckHeader styles compile [then] the chevron uses a non-negative inset inside the KEY SYNC box', () => {
	const compiled = compile(readFileSync(DECK_HEADER_PATH, 'utf8'), {
		filename: DECK_HEADER_PATH,
		generate: 'client'
	});
	assertNonNegativeInset(compiled.css.code, '.keysync-chevron');
	assert.match(
		compiled.css.code,
		/\.keysync[^{]*\{[^}]*padding-right:\s*12px/,
		'.keysync needs inset room for the chevron'
	);
	assert.doesNotMatch(
		compiled.css.code,
		/right:\s*-12px/,
		'the chevron must not hang outside the KEY SYNC box'
	);
});
