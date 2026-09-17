// requirement: OPS-33
// requirement: PERF-UI-03
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

test('BRAND_LAUNCH timing constants satisfy OPS-33 budget', async () => {
	const brandLaunch = await loadTypeScriptModule('src/lib/brand-launch.ts');
	const {
		BRAND_LAUNCH_SLIDE_MS,
		BRAND_LAUNCH_HOLD_MS,
		BRAND_LAUNCH_FADE_MS,
		BRAND_LAUNCH_DURATION_MS,
		BRAND_LAUNCH_MEET_MS,
		BRAND_LAUNCH_FADE_START_MS
	} = brandLaunch;

	assert.equal(
		BRAND_LAUNCH_DURATION_MS,
		BRAND_LAUNCH_SLIDE_MS + BRAND_LAUNCH_HOLD_MS + BRAND_LAUNCH_FADE_MS
	);
	assert.equal(BRAND_LAUNCH_MEET_MS, BRAND_LAUNCH_SLIDE_MS);
	assert.equal(BRAND_LAUNCH_FADE_START_MS, BRAND_LAUNCH_SLIDE_MS + BRAND_LAUNCH_HOLD_MS);
	assert.ok(BRAND_LAUNCH_DURATION_MS <= 1800);
	assert.ok(BRAND_LAUNCH_HOLD_MS >= 150 && BRAND_LAUNCH_HOLD_MS <= 300);
});
