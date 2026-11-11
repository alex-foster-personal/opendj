/**
 * LIBUX-11716: the compact decks setting (4-deck view library rows).
 * The HTTP half is proven in tests/webui/test_ui_prefs_settings.py.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadRuneModule } from './load-rune-module.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const ENTRY = [
	"export { setCompactDecks, uiPrefs } from '$lib/rb/prefs.svelte';",
	"export { applySettingChange, readSettingValue } from '$lib/settings/apply';"
].join('\n');

test('compact_decks is off for existing users and applySettingChange round-trips', async () => {
	const m = await loadRuneModule(ENTRY);
	assert.equal(m.uiPrefs.compact_decks, false, 'off for existing users');
	m.applySettingChange('compact_decks', true);
	assert.equal(m.readSettingValue('compact_decks'), true);
	assert.equal(m.uiPrefs.compact_decks, true);
	assert.throws(() => m.applySettingChange('compact_decks', 'yes'), /boolean/);
});

test('filterSettings finds the compact decks setting', async () => {
	const mod = await loadTypeScriptModule('src/lib/settings/search.ts');
	const hits = mod.filterSettings('compact', { hideTodo: true, group: null });
	const def = hits.keyword.find((s) => s.id === 'compact_decks');
	assert.ok(def, 'compact_decks must be searchable');
	assert.equal(def.implemented, true);
	assert.equal(def.control.kind, 'boolean');
});
