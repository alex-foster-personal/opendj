import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// The pinned Node (22.14.0) cannot import a .ts file directly, so the module is
// bundled by esbuild like every other TypeScript-backed unit test here.
let setTabLabel;

before(async () => {
	({ setTabLabel } = await loadTypeScriptModule('src/lib/components/rb/browser/playlist-set-tabs.ts'));
});

test('SET-05 setTabLabel formats name and play count', () => {
	assert.equal(setTabLabel('AtlantisParty', 3), 'AtlantisParty (3)');
});
