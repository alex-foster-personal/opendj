// requirement: LIBM-171
/**
 * The deferred boot queue drains soon after an index-based boot (Tue 6 Oct 2026).
 *
 * After #5549 the All Tracks pane painted from the library index the boot
 * prefetch had already loaded, so it never fetched a last page and nothing
 * settled the boot listing walk. Every deferred boot task then waited for the
 * scheduler's 10 s ceiling: feedback todos hydrated at 12.5 s, and the
 * comment-hotkey e2e gate (which allows 10 s) failed 22 of 34.
 *
 * Real time, real scheduler singleton, real hydration module: the only fakes
 * are the network calls. Regression lines:
 *  - if the boot index arriving does not settle the walk then a deferred task
 *    (feedback:hydrate stands in for all of them) misses its window, broken
 *  - if the ceiling has to fire then it WARNs, and this test fails on that WARN
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

test('[if] boot loads the library index [then] deferred boot work runs well before the ceiling, [else stop].', async () => {
	const boot = await loadTypeScriptModule('tests/unit/fixtures/boot-walk-settle-entry.ts');
	const warnings = [];
	const realWarn = console.warn;
	console.warn = (...args) => warnings.push(args.join(' '));
	try {
		boot.setPrefsHydratorForTests(async () => {});
		boot.setFetchBootPlaylistsForTests(() => Promise.resolve([]));
		// A library bigger than one page: the paged walk alone would not end here.
		boot.setFetchBootTracksPageForTests(() => Promise.resolve({ items: [], next_cursor: 'c1' }));
		boot.setLoadBootIndexForTests(() => Promise.resolve({ items: [] }));
		const startedAt = Date.now();
		boot.startLibraryBootHydration();

		let ranAt = null;
		boot.bootScheduler.defer('feedback:hydrate', () => (ranAt = Date.now() - startedAt));
		const budgetMs = boot.BOOT_QUIET_MS + 2_000;
		while (ranAt === null && Date.now() - startedAt < budgetMs) {
			await new Promise((resolve) => setTimeout(resolve, 50));
		}
		assert.notEqual(
			ranAt,
			null,
			`feedback:hydrate did not run within ${budgetMs} ms of boot (ceiling ${boot.DECK_LOAD_YIELD_MAX_MS} ms): the boot walk never settled`
		);
		assert.ok(ranAt >= boot.BOOT_QUIET_MS, 'the boot window still holds deferred work for its quiet period');
		assert.deepEqual(warnings, [], 'the scheduler ceiling fired');
	} finally {
		console.warn = realWarn;
		boot.resetLibraryBootHydrationForTests();
	}
});

/** Every task in the deferred boot queue on main (Tue 6 Oct 2026, PR body lists file:line). */
const DEFERRED_BOOT_TASKS = [
	'feedback-pin-layer:mount',
	'feedback:hydrate',
	'user-bauble:refreshUser',
	'account-overlay:refreshUser',
	'build-identity:fetchEngineBuild',
	'browser-panel:refresh-playlist-availability',
	'cloudsync-chip:load',
	'stem-cache-dot:load',
	'stage-overlay:prefetch',
	'hotkeys-overlay:prefetch',
	'perf-tier:fetch',
	'telemetry-consent:fetch',
	'deck-observer:flush',
	'grid-flags:scan',
	'jobs-store:hydrate',
	'machine-pressure:first',
	'usage-heartbeat:first',
	'client-samples:first'
];

test('[if] the boot walk never settles [then] the ceiling still runs every deferred task and WARNs, [else stop].', async () => {
	const boot = await loadTypeScriptModule('tests/unit/fixtures/boot-walk-settle-entry.ts');
	const warnings = [];
	const realWarn = console.warn;
	console.warn = (...args) => warnings.push(args.join(' '));
	try {
		boot.setPrefsHydratorForTests(async () => {});
		boot.setFetchBootPlaylistsForTests(() => Promise.resolve([]));
		boot.setFetchBootTracksPageForTests(() => Promise.resolve({ items: [], next_cursor: 'c1' }));
		// The settle regression itself: the index never lands, nothing ends the walk.
		boot.setLoadBootIndexForTests(() => new Promise(() => {}));
		const startedAt = Date.now();
		boot.startLibraryBootHydration();

		const ranAt = new Map();
		for (const label of DEFERRED_BOOT_TASKS) {
			boot.bootScheduler.defer(label, () => ranAt.set(label, Date.now() - startedAt));
		}
		const budgetMs = boot.BOOT_HARD_CEILING_MS + 2_000;
		while (ranAt.size < DEFERRED_BOOT_TASKS.length && Date.now() - startedAt < budgetMs) {
			await new Promise((resolve) => setTimeout(resolve, 100));
		}
		const missing = DEFERRED_BOOT_TASKS.filter((label) => !ranAt.has(label));
		assert.deepEqual(missing, [], `deferred boot work starved past ${budgetMs} ms`);
		assert.equal(warnings.length, 1, 'the ceiling firing must WARN exactly once');
		for (const label of DEFERRED_BOOT_TASKS) assert.ok(warnings[0].includes(label), `WARN names ${label}`);
	} finally {
		console.warn = realWarn;
		boot.resetLibraryBootHydrationForTests();
	}
});
