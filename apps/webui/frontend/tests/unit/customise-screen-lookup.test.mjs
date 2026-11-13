// requirement: CUSTLOOK-13878
// [if] customiseHomeFor('layout_override.performance') is not the layout screen, presets group [then] this fails, [else stop].
// [if] customiseHomeFor('audio_engine') does not return its SETTINGS_ONLY reason [then] this fails, [else stop].
// [if] an id that is not in SETTINGS_CATALOG returns instead of throwing [then] this fails, [else stop].
// [if] any implemented catalog id resolves to no home [then] this fails naming the id, [else stop].
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let lookup;
let screens;
let catalog;

before(async () => {
	lookup = await loadTypeScriptModule('src/lib/settings/customise-screen-lookup.ts');
	screens = await loadTypeScriptModule('src/lib/settings/customise-screens.ts');
	catalog = await loadTypeScriptModule('src/lib/settings/catalog.ts');
});

test('CUSTLOOK-13878 layout_override.performance is layout / presets', () => {
	assert.deepEqual(lookup.customiseHomeFor('layout_override.performance'), {
		screen: 'layout',
		group: 'presets'
	});
});

test('CUSTLOOK-13878 audio_engine returns its SETTINGS_ONLY reason', () => {
	const reason = screens.SETTINGS_ONLY.find((r) => r.id === 'audio_engine').reason;
	assert.deepEqual(lookup.customiseHomeFor('audio_engine'), { settingsOnly: reason });
});

test('CUSTLOOK-13878 an id not in the catalog throws', () => {
	assert.throws(
		() => lookup.customiseHomeFor('no_such_setting_13878'),
		/no Settings catalog entry/
	);
});

test('CUSTLOOK-13878 every implemented catalog id has a home', () => {
	for (const entry of catalog.SETTINGS_CATALOG.filter((e) => e.implemented)) {
		let home;
		assert.doesNotThrow(() => {
			home = lookup.customiseHomeFor(entry.id);
		}, `catalog id ${entry.id} has no customise home`);
		assert.ok(
			home && ('screen' in home || 'settingsOnly' in home),
			`catalog id ${entry.id} has no customise home`
		);
	}
});
