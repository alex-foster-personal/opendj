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
 * use for the sibling boot-pane decisions. Adding them there in turn pushed
 * that file past the 600-line file-size gate, so they now live in their own
 * small module, `$lib/rb/health-boot-retry.ts`, re-exported through
 * `pane-contract.svelte.ts` so BrowserPanel's import fan-out, a measured
 * hotspot, gains no new edge. This file loads that real module directly
 * through `loadTypeScriptModule`, no source-slicing, no `Function`
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
 *
 * Round 9 (BrowserPanel.svelte:876, chatgpt-codex-connector, P2 BLOCKING)
 * found a second, unguarded race: _init()'s boot Promise.all can take
 * arbitrarily long, and _refreshLibraryRowsOnce() (fired independently by a
 * library-change event or the bus's first-ever open) can write a fresher
 * allTracksCount/playlists before it resolves. _init() then applied its own
 * older snapshot unconditionally, clobbering the fresher one. reconcileBootSnapshot
 * covers that: a write-epoch counter, bumped wherever _refreshLibraryRowsOnce
 * (or anything else) writes fresher data, tells _init() whether anything beat
 * it there.
 *
 * A later round (chatgpt-codex-connector, P1 BLOCKING on this file, P2
 * BLOCKING on BrowserPanel.svelte:876) made two further points this file did
 * not yet answer:
 *
 * 1. Testing `getHealthAtBoot`/`getHealthFreshWithRetry`/`reconcileBootSnapshot`
 *    against hand-fed inputs (above) proves those functions are correct in
 *    isolation. It says nothing about whether BrowserPanel.svelte still
 *    calls them, still assigns their results to the right state, or still
 *    bumps its write-epoch counters at both real write sites - node:test
 *    cannot mount a Svelte component to check that directly (same constraint
 *    `library-refresh-coalesce.test.mjs`'s "BrowserPanel wiring" section and
 *    `browser-panel-boot-pane-stale-health-retry.test.mjs`'s round-7 fix
 *    both document), so this file was silent on it.
 * 2. The two `reconcileBootSnapshot` epoch tests above only exercise the
 *    ternary; deleting either epoch bump in BrowserPanel.svelte leaves them
 *    green (verification.md: "a mutation that stays green is a finding").
 *
 * The "BrowserPanel wiring" tests below close both: they assert, against the
 * real unmodified component source (the same technique
 * `library-refresh-coalesce.test.mjs` uses for the coalescer gate), that the
 * real call sites, the state assignments, and both epoch-bump lines are
 * actually present. Sabotage-verified: deleting any one of the asserted
 * lines from BrowserPanel.svelte turns exactly one of these tests red.
 *
 * Round 11 (chatgpt-codex-connector, P2 BLOCKING on BrowserPanel.svelte:896)
 * found that the single shared `_libraryWriteEpoch` above was itself a bug:
 * `_refreshLibraryRowsOnce()` writes `playlists` (via `_refreshPlaylists()`)
 * and `allTracksCount` (via its health re-read) at DIFFERENT times within one
 * call. A playlists-only write landing between _init()'s boot Promise.all
 * starting and resolving bumped the one shared epoch, which made _init()
 * discard its own, still-uncontested health snapshot too - not just the
 * playlists snapshot the write actually raced with. The fix splits the
 * counter into `_healthWriteEpoch` and `_playlistsWriteEpoch`, each bumped
 * only by its own field's writer and reconciled independently. The
 * "independent epochs" test below is the behavioral proof: it exercises
 * `reconcileBootSnapshot` exactly as each call site now uses it and shows the
 * two fields no longer cross-contaminate, in both directions (a
 * playlists-only write must not discard a still-fresh boot health value, and
 * a health-only write must not discard a still-fresh boot playlists value -
 * the second direction being the overshoot a fix that merely "always keep
 * the boot health value" would have introduced).
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const BROWSER_PANEL = fileURLToPath(
	new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)
);

async function _loadHealthBootRetry() {
	return loadTypeScriptModule('src/lib/rb/health-boot-retry.ts');
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

test('an unchanged write epoch applies the boot snapshot', async () => {
	const mod = await _loadHealthBootRetry();

	const result = mod.reconcileBootSnapshot({
		bootEpoch: 3,
		currentEpoch: 3,
		bootValue: 'boot-snapshot',
		currentValue: 'stale-placeholder'
	});

	assert.equal(result, 'boot-snapshot', 'nothing wrote in between, so the boot read wins');
});

test('a write epoch that advanced during the boot read keeps the fresher value', async () => {
	const mod = await _loadHealthBootRetry();

	const result = mod.reconcileBootSnapshot({
		bootEpoch: 3,
		currentEpoch: 4,
		bootValue: 'boot-snapshot',
		currentValue: 'fresher-background-write'
	});

	assert.equal(
		result,
		'fresher-background-write',
		'a background write landed while the boot read was in flight, so it must not be clobbered'
	);
});

// -------------------------------------------------------- BrowserPanel wiring

test('_init calls the real boot health/playlist read and reconciles each field through its own epoch', () => {
	const source = readFileSync(BROWSER_PANEL, 'utf8');

	assert.match(
		source,
		/import \{[\s\S]*?getHealthAtBoot[\s\S]*?\} from '\.\/browser\/pane-contract\.svelte'/,
		'getHealthAtBoot must still be imported from the real module, not reconstructed'
	);
	assert.match(
		source,
		/import \{[\s\S]*?reconcileBootSnapshot[\s\S]*?\} from '\.\/browser\/pane-contract\.svelte'/,
		'reconcileBootSnapshot must still be imported from the real module'
	);
	assert.match(
		source,
		/const bootHealthEpoch = _healthWriteEpoch;\s*const bootPlaylistsEpoch = _playlistsWriteEpoch;/,
		'_init must snapshot BOTH write epochs, independently, before starting the boot read'
	);
	// PERF-UI-03 (2942a081e) replaced the Promise.all with two eagerly started
	// promises so hydration begins during the launch animation. Both reads must
	// still start together, before the try block, and each must be awaited.
	assert.match(
		source,
		/const healthPromise = getHealthAtBoot\(getHealth\);\s*const playlistsPromise = bootPlaylistsPrefetch\(\);\s*try \{/,
		'the boot read must still call the real getHealthAtBoot, not a stand-in'
	);
	assert.match(
		source,
		/const healthRes = await healthPromise;/,
		'the boot health promise must be awaited so a rejection reaches _init\'s catch'
	);
	assert.match(
		source,
		/const lists = await playlistsPromise;/,
		'the boot playlists promise must be awaited so a rejection reaches _init\'s catch'
	);
	assert.match(
		source,
		/allTracksCount = reconcileBootSnapshot\(\{\s*bootEpoch: bootHealthEpoch,\s*currentEpoch: _healthWriteEpoch,\s*bootValue: healthRes\.health\.state_db\.tracks,\s*currentValue: allTracksCount\s*\}\);/,
		'allTracksCount must be reconciled against its OWN health epoch, not the playlists one'
	);
	assert.match(
		source,
		/playlists = reconcileBootSnapshot\(\{\s*bootEpoch: bootPlaylistsEpoch,\s*currentEpoch: _playlistsWriteEpoch,\s*bootValue: lists,\s*currentValue: playlists\s*\}\);/,
		'playlists must be reconciled against its OWN playlists epoch, not the health one'
	);
	assert.match(
		source,
		/if \(_playlistsWriteEpoch === bootPlaylistsEpoch\) \{\s*await _sweepBlankPlaylists\(lists\);\s*\}/,
		'the sweep of the boot snapshot must be skipped once something fresher has landed in playlists'
	);
});

test('_refreshLibraryRowsOnce calls the real fresh-repair read and bumps only its OWN write epoch', () => {
	const source = readFileSync(BROWSER_PANEL, 'utf8');

	assert.match(
		source,
		/import \{[\s\S]*?getHealthFreshWithRetry[\s\S]*?\} from '\.\/browser\/pane-contract\.svelte'/,
		'getHealthFreshWithRetry must still be imported from the real module'
	);
	assert.match(
		source,
		/const healthRes = await getHealthFreshWithRetry\(getHealth\);\s*allTracksCount = allTracksNonBrokenCount \?\? healthRes\.health\.state_db\.tracks;\s*_healthWriteEpoch \+= 1;/,
		'the fresh repair read must assign allTracksCount and bump the HEALTH epoch, not the playlists one, in the same block'
	);
	assert.match(
		source,
		/playlists = await listPlaylistsHydrated\(\);\s*_playlistsWriteEpoch \+= 1;\s*await _sweepBlankPlaylists\(playlists\);/,
		'_refreshPlaylists must bump the PLAYLISTS epoch, not the health one, and sweep with its own fresh playlists'
	);
	assert.match(
		source,
		/async function _refreshPlaylists\(\): Promise<void> \{\s*try \{/,
		'a failed playlist list fetch must not abort the coalesced library refresh or cancel its trailing pass'
	);
});

/**
 * Round 11's actual defect, reproduced at the `reconcileBootSnapshot` level:
 * a boot read for one field must survive a write to the OTHER field that
 * lands while it is still in flight. This is what the shared
 * `_libraryWriteEpoch` broke and the split `_healthWriteEpoch` /
 * `_playlistsWriteEpoch` fixes; BrowserPanel.svelte's actual counters are not
 * importable here (a `.svelte` component), so this drives the same
 * production function, `reconcileBootSnapshot`, with the two-epoch usage the
 * wiring tests above pin in the real source.
 */
test('a playlists-only write during the boot read must not discard the boot health value', async () => {
	const mod = await _loadHealthBootRetry();

	// _init() snapshots both epochs before its Promise.all starts.
	const bootHealthEpoch = 0;
	const bootPlaylistsEpoch = 0;

	// While the boot Promise.all is still in flight, _refreshLibraryRowsOnce
	// runs _refreshPlaylists() to completion (bumping ONLY the playlists
	// epoch) but its own health re-read has not resolved yet.
	const healthWriteEpochAfterRace = bootHealthEpoch; // unchanged: nothing wrote health yet
	const playlistsWriteEpochAfterRace = bootPlaylistsEpoch + 1; // _refreshPlaylists wrote

	const reconciledAllTracksCount = mod.reconcileBootSnapshot({
		bootEpoch: bootHealthEpoch,
		currentEpoch: healthWriteEpochAfterRace,
		bootValue: 8355, // this boot read's own, real getHealthAtBoot() result
		currentValue: null // the placeholder allTracksCount started as
	});
	const reconciledPlaylists = mod.reconcileBootSnapshot({
		bootEpoch: bootPlaylistsEpoch,
		currentEpoch: playlistsWriteEpochAfterRace,
		bootValue: ['boot-playlist'],
		currentValue: ['fresher-playlist']
	});

	assert.equal(
		reconciledAllTracksCount,
		8355,
		'a playlists-only write must not make _init() discard its own uncontested boot health value'
	);
	assert.deepEqual(
		reconciledPlaylists,
		['fresher-playlist'],
		'control: the playlists write that DID race must still win for playlists - this is not "always keep the boot value"'
	);
});

test('a health-only write during the boot read must not discard the boot playlists value', async () => {
	const mod = await _loadHealthBootRetry();

	const bootHealthEpoch = 0;
	const bootPlaylistsEpoch = 0;

	// The reverse race: the health re-read lands first, playlists have not
	// been touched by anything since boot started.
	const healthWriteEpochAfterRace = bootHealthEpoch + 1;
	const playlistsWriteEpochAfterRace = bootPlaylistsEpoch;

	const reconciledAllTracksCount = mod.reconcileBootSnapshot({
		bootEpoch: bootHealthEpoch,
		currentEpoch: healthWriteEpochAfterRace,
		bootValue: 8355,
		currentValue: 4200 // a background health write that DID race
	});
	const reconciledPlaylists = mod.reconcileBootSnapshot({
		bootEpoch: bootPlaylistsEpoch,
		currentEpoch: playlistsWriteEpochAfterRace,
		bootValue: ['boot-playlist'],
		currentValue: [] // the placeholder playlists started as
	});

	assert.equal(
		reconciledAllTracksCount,
		4200,
		'control: the health write that DID race must still win for allTracksCount'
	);
	assert.deepEqual(
		reconciledPlaylists,
		['boot-playlist'],
		'a health-only write must not make _init() discard its own uncontested boot playlists value'
	);
});
