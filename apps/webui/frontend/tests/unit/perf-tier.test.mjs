import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

test('defaults to STANDARD pending host', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/perf-tier.ts');
	assert.equal(mod.resolvedTier(), 'STANDARD');
	assert.equal(mod.prefetchTrackCap(), 4);
	assert.equal(mod.anlzEntryCap(), 32);
});

test('explicit LOW override applies caps immediately', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/perf-tier.ts');
	mod.setResolvedTier('LOW', 'override');
	assert.equal(mod.prefetchTrackCap(), 2);
	assert.equal(mod.prefetchByteCap(), 24 * 1024 * 1024);
	assert.equal(mod.anlzEntryCap(), 8);
	assert.match(mod.tierHoverSuffix(), /Active tier: LOW/);
});

test('503 leaves STANDARD with named reason', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/perf-tier.ts');
	mod.setResolvedTier('STANDARD', 'pending-host', null, null, 'tier unavailable: host_info_unavailable');
	assert.equal(mod.resolvedTier(), 'STANDARD');
	assert.match(mod.tierHoverSuffix(), /host_info_unavailable/);
});
