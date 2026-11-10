import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { SAMPLE_ROWS, createHostileEmptyHarness } from './hostile-empty-harness.mjs';

let autoPlay;
let browserFeed;

before(async () => {
	autoPlay = await loadTypeScriptModule('src/lib/rb/auto-play.ts');
	browserFeed = await loadTypeScriptModule(
		'src/lib/components/rb/browser/autoplay-browser-feed.ts'
	);
});

describe('hostile-empty AutoPlay boot fixtures', () => {
	it('[if] harness is built with no args [then] view stays empty until hydrate, [else stop].', () => {
		const h = createHostileEmptyHarness(autoPlay);
		assert.deepEqual(h.view, []);
		assert.equal(h.snapshot.active, false);
	});

	it('[if] config arrives before playlist rows [then] empty view never snapshots, [else stop].', () => {
		const feed = autoPlay.createAutoPlayFeedSnapshot();
		const atMount = browserFeed.stepAutoPlayBrowserFeed(feed, {
			enabled: true,
			playlistId: 'playlist-a',
			renderedRows: []
		});
		assert.equal(atMount.snapshotted, false);
		assert.equal(feed.active, false);
		const hydrated = browserFeed.stepAutoPlayBrowserFeed(feed, {
			enabled: true,
			playlistId: 'playlist-a',
			renderedRows: SAMPLE_ROWS.byBpm
		});
		assert.equal(hydrated.snapshotted, true);
		assert.equal(feed.active, true);
	});

	it('[if] rows arrive before config [then] enable snapshots the hydrated view, [else stop].', () => {
		const h = createHostileEmptyHarness(autoPlay);
		const pending = h.sort(SAMPLE_ROWS.byTitle);
		assert.equal(pending.snapshotted, false);
		assert.equal(h.snapshot.active, false);
		const armed = h.setEnabled(true);
		assert.equal(armed.snapshotted, true);
		assert.deepEqual(
			armed.publish.map((r) => r.stable_id),
			['alpha', 'bravo', 'charlie']
		);
	});
});
