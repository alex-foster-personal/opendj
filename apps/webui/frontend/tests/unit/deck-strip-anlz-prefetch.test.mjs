/**
 * The deck overview strip warms /anlz from an `$effect` (LIBUX-20). On a cache
 * hit `ensureAnlzPrefetch` rewrites the entry it just read, to bump its LRU
 * stamp. Tracked inside the effect, that is a read-and-write of one signal:
 * the effect re-runs on its own write until Svelte aborts the flush with
 * `effect_update_depth_exceeded`. In PR #4011's e2e gate that froze the
 * performance page (no wavestack vocal-area control, no AutoPlay stall
 * banner, 7-11 s ui-mirror stalls).
 *
 * These run the REAL rune graph (load-rune-module.mjs compiles `$effect.root`
 * with svelte's client runtime) against the REAL anlz cache, seeded through
 * its production publish path. No fetch is issued: every id is a cache hit.
 *
 * Regression lines:
 * - [if] the strip's prefetch effect re-runs on the cache entry it retouches
 *   [then] a deck holding an already-cached track loops until the flush aborts
 *   - broken
 * - [if] the tracked call does NOT loop in this harness [then] the harness is
 *   not measuring the hazard and the first check proves nothing - broken
 * - [if] the untracked prefetch stops re-running when the deck's track changes
 *   [then] a newly loaded track is never warmed - broken
 * - [if] the untracked prefetch no longer retouches the entry [then] the LRU
 *   evicts the strip the deck is showing first - broken
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadRuneModule } from './load-rune-module.mjs';

const ENTRY = `
import { flushSync } from 'svelte';
import { prefetchDeckStripAnlz } from '$lib/components/rb/deck/strip-anlz-prefetch';
import {
	ensureAnlzPrefetch,
	getAnlzEntry,
	refreshAnlzCacheEntry
} from '$lib/components/rb/wave/anlz-cache.svelte';

export { getAnlzEntry, refreshAnlzCacheEntry };

const deck = $state({ stable_id: null as string | null });

/** Mount one effect shaped like StripWaveform's warm-up and flush it. */
export function mountStripPrefetch(stableId: string | null, tracked: boolean) {
	deck.stable_id = stableId;
	let runs = 0;
	let error: unknown = null;
	const stop = $effect.root(() => {
		$effect(() => {
			runs += 1;
			const sid = deck.stable_id;
			if (tracked) {
				if (sid !== null) ensureAnlzPrefetch(sid);
			} else {
				prefetchDeckStripAnlz(sid);
			}
		});
	});
	try {
		flushSync();
	} catch (caught) {
		error = caught;
	}
	return {
		runs: () => runs,
		error: () => error,
		loadTrack(next: string | null) {
			deck.stable_id = next;
			try {
				flushSync();
			} catch (caught) {
				error = caught;
			}
		},
		stop
	};
}
`;

let mod;

before(async () => {
	mod = await loadRuneModule(ENTRY);
});

function anlzPayload(stable_id) {
	return {
		stable_id,
		points: 38400,
		waveform: {
			kind: 'mono',
			preview: { length: 0, low: [], mid: [], high: [] },
			detail: { length: 0, low: [], mid: [], high: [] }
		},
		beatgrid: { beat_count: 0, beats: [] },
		cues: [],
		phrases: [],
		local_waveform: { status: 'decoded', reason: null, preview_b64: 'AAAA', preview_max: 200 },
		vocals: { status: 'not_analyzed' }
	};
}

/** A ready, non-retryable entry: exactly what a deck-loaded track leaves behind. */
function seedReady(stable_id) {
	mod.refreshAnlzCacheEntry(stable_id, anlzPayload(stable_id));
	const entry = mod.getAnlzEntry(stable_id);
	assert.equal(entry?.status, 'ready', `seeding ${stable_id} must leave a ready entry`);
	return entry;
}

test('control: a TRACKED ensureAnlzPrefetch in an effect loops on a cached track', () => {
	seedReady('tracked-hit');
	const mounted = mod.mountStripPrefetch('tracked-hit', true);
	try {
		const error = mounted.error();
		assert.ok(
			error !== null || mounted.runs() > 100,
			`if the tracked call does not loop here then the harness is not measuring the hazard (runs=${mounted.runs()})`
		);
		if (error !== null) {
			assert.match(String(error?.message ?? error), /effect_update_depth_exceeded/);
		}
	} finally {
		mounted.stop();
	}
});

test('the strip warm-up runs ONCE for a deck holding an already-cached track', () => {
	seedReady('cached-hit');
	const mounted = mod.mountStripPrefetch('cached-hit', false);
	try {
		assert.equal(
			mounted.error(),
			null,
			'if the strip prefetch effect re-runs on its own retouch then the flush aborts - broken'
		);
		assert.equal(mounted.runs(), 1, 'one track, one warm-up');
	} finally {
		mounted.stop();
	}
});

test('the untracked warm-up still retouches the cached entry for the LRU', () => {
	const before = seedReady('lru-hit');
	const mounted = mod.mountStripPrefetch('lru-hit', false);
	try {
		const after = mod.getAnlzEntry('lru-hit');
		assert.ok(
			after.touched > before.touched,
			'if the warm-up stops retouching the entry then the LRU evicts the strip on screen first - broken'
		);
	} finally {
		mounted.stop();
	}
});

test('loading a different track re-runs the warm-up exactly once more', () => {
	seedReady('first-track');
	seedReady('second-track');
	const mounted = mod.mountStripPrefetch('first-track', false);
	try {
		assert.equal(mounted.runs(), 1);
		const secondBefore = mod.getAnlzEntry('second-track').touched;
		mounted.loadTrack('second-track');
		assert.equal(mounted.error(), null);
		assert.equal(
			mounted.runs(),
			2,
			'if a track change does not re-run the warm-up then a newly loaded track is never warmed - broken'
		);
		assert.ok(mod.getAnlzEntry('second-track').touched > secondBefore, 'and it warmed the NEW track');
	} finally {
		mounted.stop();
	}
});

test('an empty deck warms nothing and runs once', () => {
	const mounted = mod.mountStripPrefetch(null, false);
	try {
		assert.equal(mounted.error(), null);
		assert.equal(mounted.runs(), 1);
	} finally {
		mounted.stop();
	}
});
