import assert from 'node:assert/strict';
import { after, before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://trackify-feed.example.test';

function jsonResponse(body) {
	return new Response(JSON.stringify(body), {
		status: 200,
		headers: { 'content-type': 'application/json' }
	});
}

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

	describe('pagination hard limit', () => {
		let feed;
		let originalFetch;

		before(async () => {
			feed = await loadTypeScriptModule('src/lib/rb/trackify-feed.ts', {
				viteApiBase: API_BASE
			});
			originalFetch = globalThis.fetch;
		});

		after(() => {
			globalThis.fetch = originalFetch;
		});

		it('raises instead of silently truncating an all-tracks feed with a live continuation past the page cap', async () => {
			// Sol review, PR #3676: a feed that never terminates its cursor must
			// fail loud rather than be presented as complete at the 200-page cap.
			globalThis.fetch = async () =>
				jsonResponse({
					items: [{ stable_id: 'x', key: null, bpm: null, file_exists: true, file_availability: 'AVAILABILITY_PRESENT', has_rb_mapping: true }],
					next_cursor: 'still-more'
				});

			await assert.rejects(() => feed.fetchTrackifyViewRows(null), /exceeds .* rows/);
		});

		it('raises instead of silently truncating a playlist feed with a live continuation past the page cap', async () => {
			globalThis.fetch = async () =>
				new Response(
					JSON.stringify({
						tracks: [
							{
								stable_id: 'x',
								key: null,
								bpm: null,
								file_exists: true,
								file_availability: 'AVAILABILITY_PRESENT',
								has_rb_mapping: true
							}
						],
						next_offset: 500,
						total: 1
					}),
					{
						status: 200,
						headers: { 'content-type': 'application/json', etag: 'W/"1"' }
					}
				);

			await assert.rejects(
				() =>
					feed.fetchTrackifyViewRows({ playlist_id: 'pl-1', name: 'Warmup', kind: 'playlist' }),
				/exceeds .* rows/
			);
		});

		it('returns normally once a feed continuation actually ends', async () => {
			let calls = 0;
			globalThis.fetch = async () => {
				calls += 1;
				return jsonResponse({
					items: [{ stable_id: 'x', key: null, bpm: null, file_exists: true, file_availability: 'AVAILABILITY_PRESENT', has_rb_mapping: true }],
					next_cursor: calls < 3 ? 'more' : null
				});
			};

			const rows = await feed.fetchTrackifyViewRows(null);
			assert.equal(rows.length, 3);
			assert.equal(calls, 3);
		});
	});
});
