import assert from 'node:assert/strict';
import test from 'node:test';

import { setTabLabel } from '../../src/lib/components/rb/browser/playlist-set-tabs.ts';

test('SET-05 setTabLabel formats name and play count', () => {
	assert.equal(setTabLabel('AtlantisParty', 3), 'AtlantisParty (3)');
});
