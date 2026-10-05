/**
 * STEM-43: the stem cache low-disk dot. Imports and calls the exact policy
 * module `StemCacheHealthDot.svelte` runs - no source slicing, no copy.
 *
 * [if] the engine reports low_disk with a blocked_reason [then] the dot is
 * red and says why nothing was evicted [⛔️ if it reads as "being handled"].
 * [if] low_disk with no blocker [then] amber, eviction in progress.
 * [if] low_disk and eviction is merely paused in settings [then] amber, and
 * the hover says what turning it on would evict [⛔️ if a pause reads as a fault].
 * [if] low_disk at all [then] the hover says the evicted bundles stay in the cloud.
 * [if] healthy [then] green, even when bundles are still waiting to upload
 * [⛔️ if a healthy disk is painted as a warning].
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const GIB = 1024 ** 3;
let policy;

before(async () => {
	policy = await loadTypeScriptModule('src/lib/rb/stem-cache-health.ts');
});

function status(overrides = {}) {
	return {
		state: 'healthy',
		disk_free_bytes: 120 * GIB,
		floor_bytes: 46 * GIB,
		shortfall_bytes: 0,
		cache_bytes: 62 * GIB,
		bundle_count: 1596,
		local_only_count: 0,
		would_evict_count: 0,
		would_evict_bytes: 0,
		blocked_reason: null,
		last_error: null,
		...overrides
	};
}

test('loading until the first status lands', () => {
	const dot = policy.stemCacheHealthDot(null, null);
	assert.equal(dot.label, 'Stem cache disk');
	assert.equal(dot.state, 'loading');
});

test('a failed fetch is grey unknown, not a stale green and not a red verdict', () => {
	const dot = policy.stemCacheHealthDot(status(), 'engine unreachable');
	assert.equal(dot.state, 'unavailable');
	assert.equal(dot.detail, 'unknown - engine unreachable');
});

test('healthy disk is green and quotes free space against the floor', () => {
	const dot = policy.stemCacheHealthDot(status(), null);
	assert.equal(dot.state, 'complete');
	assert.match(dot.detail, /120 GiB free, floor 46 GiB/);
	assert.match(dot.detail, /stems use 62 GiB in 1596 bundles/);
});

test('healthy stays green when bundles are waiting to upload, and says so', () => {
	const dot = policy.stemCacheHealthDot(status({ local_only_count: 14 }), null);
	assert.equal(dot.state, 'complete');
	assert.match(dot.detail, /14 bundles not yet uploaded \(kept, queued for upload\)/);
});

test('low disk with no blocker is amber: eviction is relieving it', () => {
	const dot = policy.stemCacheHealthDot(
		status({
			state: 'low_disk',
			disk_free_bytes: 4.2 * GIB,
			shortfall_bytes: 41.8 * GIB,
			would_evict_count: 1240,
			would_evict_bytes: 41.8 * GIB
		}),
		null
	);
	assert.equal(dot.state, 'incomplete');
	assert.match(dot.detail, /low disk: 42 GiB short \(4\.2 GiB free, floor 46 GiB\)/);
	assert.match(dot.detail, /evicting the 1240 least recently used stem bundles \(42 GiB\)/);
	assert.match(dot.detail, /stay available from the cloud and download again when loaded/);
});

test('low disk with eviction paused is amber, not red, and says what turning it on evicts', () => {
	const dot = policy.stemCacheHealthDot(
		status({
			state: 'low_disk',
			disk_free_bytes: 39 * GIB,
			shortfall_bytes: 7 * GIB,
			would_evict_count: 208,
			would_evict_bytes: 7 * GIB,
			blocked_reason: 'auto_evict_off'
		}),
		null
	);
	assert.equal(dot.state, 'incomplete');
	assert.match(dot.detail, /automatic eviction is paused in settings, nothing evicted/);
	assert.match(dot.detail, /Turning it on would evict the 208 least recently used stem bundles \(7\.0 GiB\)/);
	assert.match(dot.detail, /stay available from the cloud/);
});

test('one bundle is singular, and nothing evictable never promises an eviction', () => {
	const low = { state: 'low_disk', disk_free_bytes: GIB, shortfall_bytes: 45 * GIB };
	const one = policy.stemCacheHealthDot(
		status({ ...low, would_evict_count: 1, would_evict_bytes: GIB }),
		null
	);
	assert.match(one.detail, /evicting the 1 least recently used stem bundle \(1\.0 GiB\)/);
	const none = policy.stemCacheHealthDot(status({ ...low, blocked_reason: 'auto_evict_off' }), null);
	assert.match(none.detail, /would evict no stem bundle is safe to evict/);
	assert.doesNotMatch(none.detail, /stay available from the cloud/);
});

test('low disk blocked on unarmed hydration is red and names the cause', () => {
	const dot = policy.stemCacheHealthDot(
		status({
			state: 'low_disk',
			disk_free_bytes: 4 * GIB,
			shortfall_bytes: 42 * GIB,
			blocked_reason: 'hydration_not_armed'
		}),
		null
	);
	assert.equal(dot.state, 'error');
	assert.match(dot.detail, /cannot fetch stems back from the cloud yet/);
	assert.doesNotMatch(dot.detail, /evicting least recently used/);
});

test('each engine blocked_reason has its own wording, and an unknown one is shown raw', () => {
	const low = { state: 'low_disk', disk_free_bytes: GIB, shortfall_bytes: 45 * GIB };
	const armed = policy.stemCacheHealthDot(
		status({ ...low, blocked_reason: 'hydration_not_armed' }),
		null
	);
	assert.equal(armed.state, 'error');
	const stuck = policy.stemCacheHealthDot(
		status({ ...low, blocked_reason: 'not_enough_evictable_bundles', local_only_count: 1 }),
		null
	);
	assert.equal(stuck.state, 'error');
	assert.match(stuck.detail, /on a deck or not yet uploaded/);
	assert.match(stuck.detail, /1 bundle not yet uploaded/);
	const unknown = policy.stemCacheHealthDot(status({ ...low, blocked_reason: 'new_reason' }), null);
	assert.equal(unknown.state, 'error');
	assert.match(unknown.detail, /blocked: new_reason/);
});

test('an engine tick failure is surfaced even on a healthy disk', () => {
	const dot = policy.stemCacheHealthDot(
		status({ last_error: 'StemCacheSettingsError: bad floor' }),
		null
	);
	assert.equal(dot.state, 'error');
	assert.match(dot.detail, /disk check failed: StemCacheSettingsError/);
});

test('formatGib: one decimal under 10 GiB, whole numbers from 10', () => {
	assert.equal(policy.formatGib(4.24 * GIB), '4.2 GiB');
	assert.equal(policy.formatGib(0), '0.0 GiB');
	assert.equal(policy.formatGib(46 * GIB), '46 GiB');
});
