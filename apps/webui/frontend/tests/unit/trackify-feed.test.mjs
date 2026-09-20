import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

describe('trackify-feed', () => {
	it('defaults last_playlist null to all_tracks scope', async () => {
		const feed = await loadTypeScriptModule('src/lib/rb/trackify-feed.ts');
		assert.equal(feed.trackifyPlaylistScope(null), 'all_tracks');
		assert.equal(
			feed.trackifyPlaylistScope({ playlist_id: 'p1', name: 'Warmup', kind: 'playlist' }),
			'playlist:p1'
		);
	});

	it('bumps epoch when a non-empty feed snapshots', async () => {
		const feed = await loadTypeScriptModule('src/lib/rb/trackify-feed.ts');
		const controller = feed.createTrackifyFeedController();
		const first = controller.step(true, null, [
			{ stable_id: 'a', key: '8A', bpm: 120, file_exists: true }
		]);
		assert.equal(first.epoch, 1);
		assert.equal(first.snapshotted, true);
		const second = controller.step(true, null, [
			{ stable_id: 'a', key: '8A', bpm: 120, file_exists: true }
		]);
		assert.equal(second.epoch, 1);
	});
});
