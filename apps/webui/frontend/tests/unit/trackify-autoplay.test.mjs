import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const row = (stable_id, key = '8A', bpm = 124) => ({
	stable_id,
	key,
	bpm,
	file_exists: true
});

describe('trackify-autoplay pure logic', () => {
	it('quarantines failed ids and picks the next candidate', async () => {
		const mod = await loadTypeScriptModule('src/lib/rb/trackify-autoplay.ts');
		const feed = [row('bad'), row('good')];
		const next = mod.pickNextTrackifyCandidate({
			feed,
			played_ids: new Set(),
			quarantined_ids: new Set(['bad']),
			current_id: 'bad',
			current_key: '8A',
			current_bpm: 124,
			enforce_play_order: false,
			min_tempo_ratio: 0.84,
			max_tempo_ratio: 1.16,
			maximize_reach: false
		});
		assert.equal(next, 'good');
	});

	it('advances only at the end of the track, never while audible audio remains or while paused', async () => {
		const mod = await loadTypeScriptModule('src/lib/rb/trackify-autoplay.ts');
		const decide = (deck, overrides = {}) =>
			mod.shouldAdvanceTrackify({
				enabled: true,
				deck: { stable_id: 'a', duration_ms: 200_000, ...deck },
				already_triggered_for: null,
				in_flight: false,
				...overrides
			});
		// Playing: every position with audio left to hear stays on the track,
		// including the whole 16 s two-deck handoff window.
		for (const position_ms of [1000, 184_000, 199_000, 199_900]) {
			assert.equal(decide({ playing: true, position_ms }), false, `playing at ${position_ms}`);
		}
		// Paused anywhere before the end: Pause keeps playback stopped.
		for (const position_ms of [1000, 184_000, 199_000, 199_900]) {
			assert.equal(decide({ playing: false, position_ms }), false, `paused at ${position_ms}`);
		}
		// Ended: the playing audio clock reached the end, or the natural-end
		// stop parked the transport there.
		assert.equal(decide({ playing: true, position_ms: 200_000 }), true);
		assert.equal(decide({ playing: false, position_ms: 200_000 }), true);
		// Once per track, never while a load is in flight, never with an
		// unknown duration.
		assert.equal(decide({ playing: false, position_ms: 200_000 }, { already_triggered_for: 'a' }), false);
		assert.equal(decide({ playing: false, position_ms: 200_000 }, { in_flight: true }), false);
		assert.equal(decide({ playing: true, position_ms: 200_000, duration_ms: null }), false);
		assert.equal(decide({ playing: true, position_ms: 200_000 }, { enabled: false }), false);
	});

	it('PLAY-04 snapshot ignores live view mutation after activation', async () => {
		const feed = await loadTypeScriptModule('src/lib/rb/trackify-feed.ts');
		const controller = feed.createTrackifyFeedController();
		const initial = [row('alpha'), row('bravo')];
		const snap = controller.step(true, null, initial);
		assert.equal(snap.snapshotted, true);
		const mutated = controller.step(true, null, [row('charlie')]);
		assert.equal(mutated.rows.length, 2);
		assert.equal(mutated.rows[0].stable_id, 'alpha');
	});

	it('empty pre-hydration feed does not arm first pick', async () => {
		const feed = await loadTypeScriptModule('src/lib/rb/trackify-feed.ts');
		const controller = feed.createTrackifyFeedController();
		const snap = controller.step(true, null, []);
		assert.equal(snap.rows.length, 0);
		assert.equal(snap.snapshotted, false);
		const autoplay = await loadTypeScriptModule('src/lib/rb/trackify-autoplay.ts');
		const next = autoplay.pickNextTrackifyCandidate({
			feed: snap.rows,
			played_ids: new Set(),
			quarantined_ids: new Set(),
			current_id: null,
			current_key: null,
			current_bpm: null,
			enforce_play_order: false,
			min_tempo_ratio: 0.84,
			max_tempo_ratio: 1.16,
			maximize_reach: false
		});
		assert.equal(next, null);
	});
});
