/**
 * PR #1656 review round 3, thread on src/lib/api.ts:472 (really about this
 * file's consumption pattern) - a boot-time health failure must not strand
 * the browser pane blank forever.
 *
 * _init() awaits getHealth() in a Promise.all with listPlaylistsHydrated();
 * any getHealth() rejection (a 500, a network drop, or now the fetch timeout
 * this PR's coalescer adds) throws out of the try block before
 * _restoreBootPane() ever runs. _refreshLibraryRowsOnce's background refresh
 * loop explicitly `continue`s past any pane whose playlist_id is still null,
 * so a boot pane that never opened this way is never retried by anything -
 * the user sees an error toast and a blank tree until a manual reload.
 *
 * Round 7 (BrowserPanel.svelte:53/1230, chatgpt-codex-connector, P1
 * BLOCKING) rejected the version of this test that extracted
 * `_getHealthAtBoot`/`_getHealthFreshWithRetry` as text from
 * BrowserPanel.svelte and reconstructed them with `Function`: that harness
 * never executes the compiled component and can stay green with its real
 * call sites, imports, or wiring broken. The fix moved both functions out of
 * the component into `pane-contract.svelte.ts` as real, exported, pure
 * functions parameterized over `getHealth` - the same injection point
 * `resolveBootPlaylist` and `shouldRetryBootPane` in that same file already
 * use for the sibling boot-pane decisions (kept there rather than a new
 * module so BrowserPanel's import fan-out, a measured hotspot, does not grow
 * for two functions this small). This file now loads that real module
 * directly through `loadTypeScriptModule`, no source-slicing, no `Function`
 * reconstruction.
 *
 * Regression lines:
 * - if a single getHealth() failure is not retried then a merely-slow (not
 *   actually down) daemon strands the boot pane blank -> broken
 * - if the retry does not bypass the coalesced/failed cache entry (fresh:
 *   true) then the retry can rejoin the same failed promise and never truly
 *   retry -> broken
 * - if BOTH attempts fail, the second failure must still propagate (no
 *   swallowing) so _init()'s existing catch/toast path still fires -> broken
 * - the same three lines apply to the background repair read
 *   (getHealthFreshWithRetry), whose only chance to run is the bus's
 *   first-ever open: nothing else retries it once that has fired.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

async function _loadHealthBootRetry() {
	return loadTypeScriptModule('src/lib/components/rb/browser/pane-contract.svelte.ts');
}

test('a single getHealth() failure at boot is retried once with fresh:true', async () => {
	const mod = await _loadHealthBootRetry();
	const calls = [];
	const getHealth = async (options) => {
		calls.push(options ?? null);
		if (calls.length === 1) throw new Error('timed out');
		return { health: { state_db: { tracks: 5 } } };
	};

	const result = await mod.getHealthAtBoot(getHealth);

	assert.equal(calls.length, 2, 'must retry exactly once after the first failure');
	assert.equal(calls[0], null, 'the first attempt must use the normal (coalesced) call');
	assert.deepEqual(calls[1], { fresh: true }, 'the retry must bypass the cache, not rejoin the failed entry');
	assert.equal(result.health.state_db.tracks, 5);
});

test('a getHealth() success on the first attempt never retries', async () => {
	const mod = await _loadHealthBootRetry();
	const calls = [];
	const getHealth = async (options) => {
		calls.push(options ?? null);
		return { health: { state_db: { tracks: 8355 } } };
	};

	const result = await mod.getHealthAtBoot(getHealth);

	assert.equal(calls.length, 1, 'a first-attempt success must not trigger a second call');
	assert.equal(result.health.state_db.tracks, 8355);
});

test('two consecutive getHealth() failures still propagate', async () => {
	const mod = await _loadHealthBootRetry();
	const getHealth = async () => {
		throw new Error('daemon unreachable');
	};

	await assert.rejects(mod.getHealthAtBoot(getHealth), /daemon unreachable/);
});

test('a single fresh health-repair failure is retried once with fresh:true', async () => {
	const mod = await _loadHealthBootRetry();
	const calls = [];
	const getHealth = async (options) => {
		calls.push(options ?? null);
		if (calls.length === 1) throw new Error('timed out');
		return { health: { state_db: { tracks: 5 } } };
	};

	const result = await mod.getHealthFreshWithRetry(getHealth);

	assert.equal(calls.length, 2, 'must retry exactly once after the first failure');
	assert.deepEqual(calls[0], { fresh: true }, 'the first attempt must bypass the coalescer');
	assert.deepEqual(calls[1], { fresh: true }, 'the retry must bypass it too, not join the failed entry');
	assert.equal(result.health.state_db.tracks, 5);
});

test('a fresh health-repair success on the first attempt never retries', async () => {
	const mod = await _loadHealthBootRetry();
	const calls = [];
	const getHealth = async (options) => {
		calls.push(options ?? null);
		return { health: { state_db: { tracks: 8355 } } };
	};

	const result = await mod.getHealthFreshWithRetry(getHealth);

	assert.equal(calls.length, 1, 'a first-attempt success must not trigger a second call');
	assert.equal(result.health.state_db.tracks, 8355);
});

test('two consecutive fresh health-repair failures still propagate', async () => {
	const mod = await _loadHealthBootRetry();
	const getHealth = async () => {
		throw new Error('daemon unreachable');
	};

	await assert.rejects(mod.getHealthFreshWithRetry(getHealth), /daemon unreachable/);
});
