/**
 * Issue #10303: KEY SYNC ladder chevron stays inside the button box in 4-deck view.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { compile } from 'svelte/compiler';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const HEADER_PATH = fileURLToPath(
	new URL('../../src/lib/components/rb/deck/DeckHeader.svelte', import.meta.url)
);
const FAKE_NAVIGATION = fileURLToPath(new URL('./fake-app-navigation.mjs', import.meta.url));

const ENTRY = [
	"export { default as DeckHeader } from '$lib/components/rb/deck/DeckHeader.svelte';",
	"export { _emptyDeckState } from '$lib/player/state.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

let ssr;

before(async () => {
	globalThis.requestAnimationFrame = () => 0;
	globalThis.cancelAnimationFrame = () => {};
	ssr = await loadSvelteSsrModule(ENTRY, { alias: { '$app/navigation': FAKE_NAVIGATION } });
});

function noop() {
	return Promise.resolve();
}

function renderDeck1() {
	const deck = {
		...ssr._emptyDeckState(1),
		stable_id: 'fixture-track',
		title: 'Fixture',
		bpm: 128
	};
	return ssr.render(ssr.DeckHeader, {
		props: {
			deck,
			deckId: 1,
			pending: false,
			onBeatSync: noop,
			onMaster: noop,
			onMasterTempo: noop,
			onKeySync: noop,
			onKeyNudge: noop,
			onResetTempo: noop,
			onUnload: noop,
			keySyncAvailable: true,
			mirrored: false
		}
	}).body;
}

function keyPairSlice(html) {
	const open = html.indexOf('key-pair');
	assert.notEqual(open, -1, 'rendered deck header missing key-pair');
	const after = html.indexOf('key-badge', open);
	assert.notEqual(after, -1, 'rendered deck header missing key-badge after key-pair');
	return html.slice(open, after);
}

function compiledCss() {
	return compile(readFileSync(HEADER_PATH, 'utf8'), { filename: HEADER_PATH, generate: 'server' })
		.css.code;
}

test('if KEY SYNC renders then the ladder chevron sits in key-pair with no outside absolute offset, else stop', () => {
	const pair = keyPairSlice(renderDeck1());
	const keySyncOpen = pair.indexOf('data-testid="key-sync-deck-1"');
	const chevronOpen = pair.indexOf('keysync-chevron');
	assert.notEqual(keySyncOpen, -1, 'missing KEY SYNC button in markup');
	assert.notEqual(chevronOpen, -1, 'missing keysync-chevron in markup');
	assert.ok(keySyncOpen < chevronOpen, 'chevron must follow KEY SYNC inside key-pair');
	assert.doesNotMatch(pair.slice(chevronOpen), /margin:\s*0[^;]*-[0-9]/);
	assert.doesNotMatch(pair.slice(chevronOpen), /margin-[a-z]+:\s*-[0-9]/);
	const css = compiledCss();
	assert.match(css, /\.keysync-chevron[^}]*right:\s*1px/);
	assert.doesNotMatch(css, /keysync-chevron[^}]*right:\s*-/);
	assert.match(css, /\.keysync[^}]*padding-right:\s*12px/);
});

test('if deck header renders then KEY SYNC keeps the 2-deck chrome button contract, else stop', () => {
	const pair = keyPairSlice(renderDeck1());
	const keySyncClose = pair.indexOf('</button>', pair.indexOf('KEY SYNC'));
	const keySyncTag = pair.slice(pair.indexOf('<button'), keySyncClose);
	assert.match(keySyncTag, /class="rb-lit-button keysync/);
	assert.match(keySyncTag, /data-testid="key-sync-deck-1"/);
	assert.match(keySyncTag, /data-performance-control="key-sync"/);
	assert.match(pair, />KEY SYNC</);
	assert.doesNotMatch(keySyncTag, /right:\s*-/);
});
