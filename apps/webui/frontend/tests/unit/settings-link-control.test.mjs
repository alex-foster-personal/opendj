// requirement: CSUI-01
// if CloudSync link rows stop navigating from overlay or /settings then broken
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

function read(relative) {
	return readFileSync(fileURLToPath(new URL(`../../${relative}`, import.meta.url)), 'utf8');
}

let catalog;
let apply;

before(async () => {
	catalog = await loadTypeScriptModule('src/lib/settings/catalog.ts');
	apply = await loadTypeScriptModule('src/lib/settings/apply.ts');
});

test('SettingsOverlay renders link controls and activateSetting navigates', () => {
	const overlay = read('src/lib/components/settings/SettingsOverlay.svelte');
	assert.match(overlay, /def\.control\.kind === 'link'/);
	assert.match(overlay, /<a[\s\S]*href=\{def\.control\.href\}/);
	assert.match(overlay, /def\.control\.kind === 'link'[\s\S]*goto\(/);
	assert.match(overlay, /def\.control\.kind === 'link'[\s\S]*closeSettings\(\)/);
});

test('/settings page links come from catalog without hardcoded hrefs', () => {
	const page = read('src/routes/settings/+page.svelte');
	assert.match(page, /\$lib\/settings\/catalog/);
	assert.match(page, /<a href=\{def\.control\.href\}/);
	assert.doesNotMatch(page, /\/cloudsync\?tab=/);
});

test('implemented link catalog rows are not writable settings keys', () => {
	for (const def of catalog.SETTINGS_CATALOG) {
		if (def.implemented && def.control.kind === 'link') {
			assert.equal(
				apply.isAllowedSettingKey(def.id),
				false,
				`${def.id} must not be in ALLOWED_SETTING_KEYS`
			);
		}
	}
});
