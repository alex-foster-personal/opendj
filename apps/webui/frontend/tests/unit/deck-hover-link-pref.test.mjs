/**
 * DECKUX-5917b: the deck hover link highlight (deck, wavestack row, mixer strip,
 * library row lighting up together) can be turned off in Settings.
 * The HTTP half (GET/PUT /api/v1/ui-prefs) is proven against the real route in
 * tests/webui/test_ui_prefs_settings.py; this file covers the client state.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { loadRuneModule } from './load-rune-module.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const ENTRY = [
	"export { deckHoverLinkId, deckHoverUi, setHoveredDeck } from '$lib/rb/deck-hover.svelte';",
	"export { setDeckHoverLink, uiPrefs } from '$lib/rb/prefs.svelte';",
	"export { applySettingChange, readSettingValue } from '$lib/settings/apply';"
].join('\n');

test('the link highlight follows the hovered deck by default and stops when the pref is off', async () => {
	const m = await loadRuneModule(ENTRY);
	assert.equal(m.uiPrefs.deck_hover_link, true, 'default must stay on for new users');

	m.setHoveredDeck(2);
	assert.equal(m.deckHoverLinkId(m.uiPrefs.deck_hover_link), 2, 'pref on: the hovered deck draws the link highlight');

	m.applySettingChange('deck_hover_link', false);
	assert.equal(m.readSettingValue('deck_hover_link'), false);
	assert.equal(m.uiPrefs.deck_hover_link, false);
	assert.equal(m.deckHoverLinkId(m.uiPrefs.deck_hover_link), null, 'pref off: no deck draws the link highlight');
	// Control for the overshoot: hotkeys and playhead logic still need the hovered deck.
	assert.equal(m.deckHoverUi.deckId, 2, 'pref off must not stop tracking the pointer');
	m.setHoveredDeck(3);
	assert.equal(m.deckHoverUi.deckId, 3, 'pref off: hovering another deck is still tracked');
	assert.equal(m.deckHoverLinkId(m.uiPrefs.deck_hover_link), null);

	m.setDeckHoverLink(true);
	assert.equal(m.deckHoverLinkId(m.uiPrefs.deck_hover_link), 3, 'turning it back on restores the highlight');
});

test('every panel that draws the link highlight reads it through deckHoverLinkId', () => {
	const hover = readFileSync(new URL('../../src/lib/rb/deck-hover.svelte.ts', import.meta.url), 'utf8');
	assert.doesNotMatch(hover, /from '\$lib\/rb\/prefs\.svelte'/, 'deck-hover must not add an importer to prefs.svelte (frontend.max_fan_in)');
	const read = (rel) =>
		readFileSync(new URL(`../../src/lib/components/rb/${rel}`, import.meta.url), 'utf8');
	assert.match(read('Deck.svelte'), /class:deck-focus=\{deckHoverLinkId\(uiPrefs\.deck_hover_link\) === deckId\}/);
	assert.match(read('wave/WaveRow.svelte'), /class:deck-focus=\{deckHoverLinkId\(uiPrefs\.deck_hover_link\) === deckId\}/);
	assert.match(read('mixer/ChannelStrip.svelte'), /focused = \$derived\(deckHoverLinkId\(uiPrefs\.deck_hover_link\) === deckId\)/);
	assert.match(read('browser/TrackTable.svelte'), /hoverDeckId = \$derived\(deckHoverLinkId\(uiPrefs\.deck_hover_link\)\)/);
	for (const rel of ['Deck.svelte', 'wave/WaveRow.svelte', 'mixer/ChannelStrip.svelte']) {
		assert.doesNotMatch(
			read(rel),
			/deck-focus=\{deckHoverUi\.deckId/,
			`${rel} must not bypass the pref with the raw hovered deck`
		);
	}
});

test('filterSettings finds the deck hover link setting', async () => {
	const mod = await loadTypeScriptModule('src/lib/settings/search.ts');
	const hits = mod.filterSettings('hover highlight', { hideTodo: true, group: null });
	const def = hits.keyword.find((s) => s.id === 'deck_hover_link');
	assert.ok(def, 'deck_hover_link must be searchable');
	assert.equal(def.implemented, true);
	assert.equal(def.control.kind, 'boolean');
});

