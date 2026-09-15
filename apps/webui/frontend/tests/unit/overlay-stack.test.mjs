/**
 * Overlay z-index invariants (issue #2722): setup and settings must sit above
 * the boot preflight gate so interactive surfaces are never buried.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

test('overlay stack ordering keeps interactive surfaces above the boot gate', async () => {
	const { OVERLAY_Z } = await loadTypeScriptModule('src/lib/overlays/stack.ts', {
		viteApiBase: 'https://overlay-stack.example.test'
	});

	assert.ok(OVERLAY_Z.setupPanel > OVERLAY_Z.preflightBoot);
	assert.ok(OVERLAY_Z.settings > OVERLAY_Z.preflightBoot);
	assert.ok(OVERLAY_Z.brandLaunch > OVERLAY_Z.setupPanel);
	assert.ok(OVERLAY_Z.brandLaunch > OVERLAY_Z.settings);
});
