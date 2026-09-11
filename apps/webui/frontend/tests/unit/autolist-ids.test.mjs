import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let ids;

before(async () => {
	ids = await loadTypeScriptModule('src/lib/components/rb/browser/autolist-ids.ts');
});

test('isAutolistId recognizes sentinel', () => {
	assert.equal(ids.isAutolistId('autolist'), true);
	assert.equal(ids.isAutolistId('missing'), false);
	assert.equal(ids.isAutolistId('pl-uuid'), false);
});
