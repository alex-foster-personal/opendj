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

//-----------------------------------------------------------------------------
// fetchPerfTier: a 200 must carry a real tier
//-----------------------------------------------------------------------------

const PERF_TIER_API_BASE = 'https://perf-tier-client.example.test';

async function _withPerfTierResponse(status, body, run) {
	const client = await loadTypeScriptModule('tests/unit/fixtures/perf-tier-client-entry.ts', {
		viteApiBase: PERF_TIER_API_BASE
	});
	const originalFetch = globalThis.fetch;
	const requested = [];
	globalThis.fetch = async (url) => {
		requested.push(String(url));
		return new Response(JSON.stringify(body), {
			status,
			headers: { 'content-type': 'application/json' }
		});
	};
	try {
		await run(client);
	} finally {
		globalThis.fetch = originalFetch;
	}
	assert.deepEqual(requested, [`${PERF_TIER_API_BASE}${client.PERF_TIER_PATH}`]);
}

// if fetchPerfTier accepts a 200 with no tier then setResolvedTier stores undefined as the active tier
test('a 200 whose body has no tier rejects loudly, naming the body, and leaves the tier untouched', async () => {
	await _withPerfTierResponse(200, { source: 'auto', auto_tier: null, override: 'auto' }, async (client) => {
		await assert.rejects(client.fetchPerfTier(), (error) => {
			assert.ok(error instanceof Error);
			assert.match(error.message, /no valid tier/);
			assert.match(error.message, /LOW, STANDARD, HIGH/);
			assert.match(error.message, /"source":"auto"/);
			return true;
		});
		assert.equal(client.resolvedTier(), 'STANDARD');
		assert.equal(client.resolvedTierSource(), 'pending-host');
	});
});

// if fetchPerfTier accepts an unknown tier name then activeScalers() returns undefined caps
test('a 200 whose tier is not a known tier name rejects loudly', async () => {
	await _withPerfTierResponse(200, { tier: 'ULTRA', source: 'auto', auto_tier: 'ULTRA', override: 'auto' }, async (client) => {
		await assert.rejects(client.fetchPerfTier(), /no valid tier.*"tier":"ULTRA"/);
		assert.equal(client.resolvedTier(), 'STANDARD');
	});
});

// if the validation also refuses real tiers then a healthy engine never resolves its tier
test('control: a 200 carrying a valid tier still resolves it', async () => {
	await _withPerfTierResponse(
		200,
		{ tier: 'HIGH', source: 'auto', auto_tier: 'HIGH', override: 'auto', host: { logical_cpus: 16, ram_bytes: 64 * 1024 ** 3 } },
		async (client) => {
			await client.fetchPerfTier();
			assert.equal(client.resolvedTier(), 'HIGH');
			assert.equal(client.resolvedTierSource(), 'auto');
			assert.equal(client.perfTierFaultReason(), null);
		}
	);
});
