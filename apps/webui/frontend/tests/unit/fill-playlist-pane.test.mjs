import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let contract;
let fill;

before(async () => {
	contract = await loadTypeScriptModule(
		'src/lib/components/rb/browser/pane-contract.svelte.ts'
	);
	fill = await loadTypeScriptModule(
		'src/lib/components/rb/browser/fill-playlist-pane.ts'
	);
});

function mapRow(item, order) {
	return { stable_id: String(item), order };
}

test('first page clears loading before the second page fetch', async () => {
	const p = contract.createPaneStore();
	const seq = p.beginLoad('pl-1', 'Warmup');
	let fetchCalls = 0;
	let releasePage2;
	const page2Gate = new Promise((resolve) => {
		releasePage2 = resolve;
	});
	let painted;
	const paintedP = new Promise((resolve) => {
		painted = resolve;
	});

	const walk = fill.fillPlaylistPane({
		pane: p,
		seq,
		fetchPage: async (offset) => {
			fetchCalls += 1;
			if (fetchCalls === 1) {
				assert.equal(offset, 0);
				return {
					page: { tracks: ['a', 'b'], total: 4, next_offset: 2 },
					etag: '"etag-1"'
				};
			}
			await page2Gate;
			return {
				page: { tracks: ['c', 'd'], total: 4, next_offset: null },
				etag: '"etag-1"'
			};
		},
		mapRow,
		progressTotal: 4,
		onFirstPaint: () => {
			assert.equal(fetchCalls, 1);
			assert.equal(p.loading, false);
			assert.equal(p.etag, '"etag-1"');
			painted();
		}
	});

	await paintedP;
	releasePage2();
	await walk;

	assert.deepEqual(p.rows.map((r) => r.stable_id), ['a', 'b', 'c', 'd']);
	assert.equal(p.load_progress, null);
});
