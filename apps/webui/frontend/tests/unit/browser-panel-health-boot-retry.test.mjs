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
 * The node:test harness cannot mount a Svelte component, so this evaluates
 * BrowserPanel's real `_getHealthAtBoot` implementation directly from its
 * source (same technique as browser-panel-superseded-load-toast.test.mjs).
 *
 * Regression lines:
 * - if a single getHealth() failure is not retried then a merely-slow (not
 *   actually down) daemon strands the boot pane blank -> broken
 * - if the retry does not bypass the coalesced/failed cache entry (fresh:
 *   true) then the retry can rejoin the same failed promise and never truly
 *   retry -> broken
 * - if BOTH attempts fail, the second failure must still propagate (no
 *   swallowing) so _init()'s existing catch/toast path still fires -> broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const PANEL = fileURLToPath(
	new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)
);

/** Builds the real `_getHealthAtBoot`, evaluated straight from
 * BrowserPanel.svelte, with `getHealth` supplied as a factory argument so it
 * can run outside the component. */
function makeGetHealthAtBoot(getHealth) {
	const source = readFileSync(PANEL, 'utf8');
	const marker = '\tasync function _getHealthAtBoot(';
	const start = source.indexOf(marker);
	assert.ok(start >= 0, 'could not find BrowserPanel._getHealthAtBoot');
	const end = source.indexOf('\n\tasync function _init(', start);
	assert.ok(end > start, 'could not isolate BrowserPanel._getHealthAtBoot');
	const functionSource = source
		.slice(start, end)
		.replace(
			/async function _getHealthAtBoot\(\)[^{]*\{/,
			'async function _getHealthAtBoot() {'
		);
	assert.doesNotMatch(functionSource, /ReturnType<|: Promise</, 'TypeScript annotation survived stripping');
	const factory = Function('getHealth', `${functionSource}\nreturn _getHealthAtBoot;`);
	return factory(getHealth);
}

test('a single getHealth() failure at boot is retried once with fresh:true', async () => {
	const calls = [];
	const getHealthAtBoot = makeGetHealthAtBoot(async (options) => {
		calls.push(options ?? null);
		if (calls.length === 1) throw new Error('timed out');
		return { health: { state_db: { tracks: 5 } } };
	});

	const result = await getHealthAtBoot();

	assert.equal(calls.length, 2, 'must retry exactly once after the first failure');
	assert.equal(calls[0], null, 'the first attempt must use the normal (coalesced) call');
	assert.deepEqual(calls[1], { fresh: true }, 'the retry must bypass the cache, not rejoin the failed entry');
	assert.equal(result.health.state_db.tracks, 5);
});

test('a getHealth() success on the first attempt never retries', async () => {
	const calls = [];
	const getHealthAtBoot = makeGetHealthAtBoot(async (options) => {
		calls.push(options ?? null);
		return { health: { state_db: { tracks: 8355 } } };
	});

	const result = await getHealthAtBoot();

	assert.equal(calls.length, 1, 'a first-attempt success must not trigger a second call');
	assert.equal(result.health.state_db.tracks, 8355);
});

test('two consecutive getHealth() failures still propagate', async () => {
	const getHealthAtBoot = makeGetHealthAtBoot(async () => {
		throw new Error('daemon unreachable');
	});

	await assert.rejects(getHealthAtBoot(), /daemon unreachable/);
});
