/**
 * Issue #13133 - pins the four-mode app dropdown acceptance lines (PERFMODE-13):
 * the current mode is marked when the menu opens, selecting an available mode
 * persists it and follows its route, and no unavailable-mode branch exists.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const { APP_MODES } = await loadTypeScriptModule('src/lib/rb/app-mode.ts');
const { APP_MODE_PREF_IDS, validateAppModePrefField } = await loadTypeScriptModule(
	'src/lib/rb/app-mode-prefs.ts'
);

const topBar = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/TopBar.svelte', import.meta.url)),
	'utf8'
);

/** Returns the `{#each APP_MODES ...}` mode-menu block of TopBar. */
function modeMenuBlock() {
	const start = topBar.indexOf('{#each APP_MODES as mode');
	assert.notEqual(start, -1, 'TopBar must iterate APP_MODES in the mode menu');
	const end = topBar.indexOf('{/each}', start);
	assert.notEqual(end, -1, 'mode menu each block must close');
	return topBar.slice(start, end);
}

test('opening the menu marks the current mode, derived from the route', () => {
	const block = modeMenuBlock();
	assert.match(block, /aria-current=\{mode\.id === activeMode\.id \? 'page' : undefined\}/);
	assert.doesNotMatch(block, /mode\.id === 'performance' \? 'page'/);
	assert.match(topBar, /return appModeForPath\(\$page\.url\.pathname\);/);
	assert.match(topBar, /APP_MODES\.find\(\(mode\) => mode\.id === uiPrefs\.app_mode\)/);
});

test('selecting an available mode persists its id and follows its href', () => {
	const block = modeMenuBlock();
	assert.match(block, /<a\s[^>]*href=\{mode\.href\}/);
	assert.match(block, /onclick=\{\(\) => _selectAppMode\(mode\.id\)\}/);
	assert.match(
		topBar,
		/function _selectAppMode\(modeId[\s\S]*?\): void \{[\s\S]*?setAppMode\(modeId\);/
	);
	assert.deepEqual(
		[...APP_MODE_PREF_IDS].sort(),
		APP_MODES.map((mode) => mode.id).sort(),
		'every selectable mode id is a valid ui-prefs app_mode value'
	);
	for (const mode of APP_MODES) {
		assert.deepEqual(validateAppModePrefField({ app_mode: mode.id }, 'k'), {
			app_mode: mode.id
		});
	}
});

test('no unavailable mode is listed: all four rows are available links', () => {
	for (const mode of APP_MODES) {
		assert.equal('unavailableReason' in mode, false, `${mode.id} has no unavailable reason`);
	}
	const block = modeMenuBlock();
	assert.doesNotMatch(block, /<button/);
	assert.doesNotMatch(block, /disabled=/);
	assert.doesNotMatch(block, /unavailableReason/);
	assert.throws(() => validateAppModePrefField({ app_mode: 'not-a-mode' }, 'k'), /app_mode/);
});
