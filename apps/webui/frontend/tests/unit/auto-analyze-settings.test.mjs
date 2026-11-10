// requirement: ANALYSIS-11724
// [if] analyze-on-import lane settings are absent from the catalog [then] broken
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let catalog;

before(async () => {
	catalog = await loadTypeScriptModule('src/lib/settings/catalog.ts');
});

const IDS = [
	'auto_analyze_grid',
	'auto_analyze_key',
	'auto_analyze_sections',
	'auto_analyze_vocals'
];

test('the four analyze-on-import settings live in group analysis', () => {
	for (const id of IDS) {
		const row = catalog.SETTINGS_CATALOG.find((s) => s.id === id);
		assert.ok(row, `${id} is in SETTINGS_CATALOG`);
		assert.equal(row.group, 'analysis');
		assert.equal(row.implemented, true);
		assert.deepEqual(row.control, { kind: 'boolean' });
	}
	assert.ok(catalog.SETTING_GROUPS.some((g) => g.id === 'analysis'));
});
