// requirement: PERF-UI-03
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

test('BRAND_LAUNCH_DURATION_MS is 2700', async () => {
	const brandLaunch = await loadTypeScriptModule('src/lib/brand-launch.ts');
	assert.equal(brandLaunch.BRAND_LAUNCH_DURATION_MS, 2700);
});
