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

	it('arms advance only inside the remaining window', async () => {
		const mod = await loadTypeScriptModule('src/lib/rb/trackify-autoplay.ts');
		assert.equal(
			mod.shouldAdvanceTrackify({
				enabled: true,
				deck: { stable_id: 'a', playing: true, position_ms: 1000, duration_ms: 200_000 },
				already_triggered_for: null,
				in_flight: false
			}),
			false
		);
		assert.equal(
			mod.shouldAdvanceTrackify({
				enabled: true,
				deck: { stable_id: 'a', playing: true, position_ms: 199_000, duration_ms: 200_000 },
				already_triggered_for: null,
				in_flight: false
			}),
			true
		);
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
