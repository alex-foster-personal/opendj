/**
 * @pytest.mark.requirement LIBUX-31
 * Pin 36e2e2a7ccff: a remembered "do this every time" choice needs a place in
 * settings to change or reset it. The playlist-drop choice (add or move) could
 * be remembered from its prompt but had no settings row, so it could never be
 * undone.
 *
 * [if] nothing is remembered [then] the setting reads `ask`
 * [if] the setting is put back to `ask` [then] the remembered choice is
 *   cleared, so the prompt returns
 * [if] the setting is given a value that is not ask, add or move [then] it is
 *   refused loudly
 *
 * Regression lines:
 * - if the catalog loses the row then the remembered choice is permanent again
 * - if `ask` stores a value instead of clearing then the prompt never returns
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const ID = 'confirm.playlist_drop_mode';
const APPLY_SRC = readFileSync(new URL('../../src/lib/settings/apply.ts', import.meta.url), 'utf8');

let dropMode;
let catalog;
let apply;

before(async () => {
	const map = new Map();
	globalThis.window = {
		localStorage: {
			getItem: (key) => (map.has(key) ? map.get(key) : null),
			setItem: (key, value) => map.set(key, String(value)),
			removeItem: (key) => map.delete(key)
		},
		addEventListener() {},
		removeEventListener() {}
	};
	dropMode = await loadTypeScriptModule('src/lib/settings/confirm-drop-mode.ts');
	catalog = await loadTypeScriptModule('src/lib/settings/catalog.ts');
	apply = await loadTypeScriptModule('src/lib/settings/apply.ts');
});

test('nothing remembered reads as ask; a remembered choice reads as itself', () => {
	assert.equal(dropMode.dropModeSettingValue(undefined), 'ask');
	assert.equal(dropMode.dropModeSettingValue('add'), 'add');
	assert.equal(dropMode.dropModeSettingValue('move'), 'move');
});

test('ask clears the remembered choice; add and move store it', () => {
	assert.equal(dropMode.dropModePrefFromSetting('ask'), undefined);
	assert.equal(dropMode.dropModePrefFromSetting('add'), 'add');
	assert.equal(dropMode.dropModePrefFromSetting('move'), 'move');
});

test('any other value is refused, never stored', () => {
	for (const bad of ['copy', '', true, 'Ask']) {
		assert.throws(() => dropMode.dropModePrefFromSetting(bad), /ask\|add\|move/);
	}
});

test('the settings panel carries the row with all three options', () => {
	const def = catalog.SETTINGS_CATALOG.find((row) => row.id === ID);
	assert.ok(def, `${ID} is not in the settings catalog`);
	assert.equal(def.implemented, true);
	assert.equal(def.group, 'confirmations');
	assert.equal(def.control.kind, 'enum');
	assert.deepEqual(
		def.control.options.map((option) => option.value),
		['ask', 'add', 'move']
	);
});

test('the row is allowlisted and reads ask on a fresh profile', () => {
	assert.equal(apply.isAllowedSettingKey(ID), true);
	assert.equal(apply.readSettingValue(ID), 'ask');
});

test('a panel write goes through the mapper into the confirm pref', () => {
	assert.match(
		APPLY_SRC,
		/case 'confirm\.playlist_drop_mode':\s*setConfirmPref\('playlist_drop_mode', dropModePrefFromSetting\(value\)\);/
	);
});

test('searching for the prompt words finds the row', async () => {
	const { filterSettings } = await loadTypeScriptModule('src/lib/settings/search.ts');
	for (const query of ['confirm', 'drop', 'remember']) {
		const { all } = filterSettings(query, { hideTodo: true, group: null });
		assert.ok(all.some((def) => def.id === ID), `searching ${query} does not find ${ID}`);
	}
});
