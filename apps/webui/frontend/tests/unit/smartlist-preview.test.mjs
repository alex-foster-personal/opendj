import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/smartlist-preview.ts');
});

test('SMARTLIST_PREVIEW_CAP is 2000', () => {
	assert.equal(mod.SMARTLIST_PREVIEW_CAP, 2000);
});

test('smartlistMatchIsTruncated is true only when total exceeds loaded', () => {
	assert.equal(mod.smartlistMatchIsTruncated(100, 5000), true);
	assert.equal(mod.smartlistMatchIsTruncated(2000, 2000), false);
	assert.equal(mod.smartlistMatchIsTruncated(10, 4), false);
});

test('smartlistTruncationNote uses en-US grouping', () => {
	assert.equal(
		mod.smartlistTruncationNote(2000, 5000),
		'Showing first 2,000 of 5,000 matching tracks'
	);
});
