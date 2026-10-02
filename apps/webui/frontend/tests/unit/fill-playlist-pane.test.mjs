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

// LIBM-134: after a small first page (L3 first paint), fill pages are large so a
// 10k-member playlist fills in ~21 requests instead of ~335.

function memberServer(n) {
	const members = Array.from({ length: n }, (_, i) => `m${String(i).padStart(5, '0')}`);
	const calls = [];
	const fetchPage = async (offset, limit) => {
		calls.push({ offset, limit });
		const tracks = members.slice(offset, offset + limit);
		const next = offset + tracks.length;
		return {
			page: { tracks, total: n, next_offset: next >= n ? null : next },
			etag: '"e"'
		};
	};
	return { members, calls, fetchPage };
}

async function fillAll(n) {
	const server = memberServer(n);
	const p = contract.createPaneStore();
	const seq = p.beginLoad('pl-big', 'Big');
	await fill.fillPlaylistPane({
		pane: p,
		seq,
		fetchPage: server.fetchPage,
		mapRow,
		progressTotal: n
	});
	return { server, p };
}

test('LIBM-134 first request is the small first page, every later request is a fill page', async () => {
	const { server } = await fillAll(10042);
	assert.equal(fill.PLAYLIST_FIRST_PAGE, 30);
	assert.equal(fill.PLAYLIST_FILL_PAGE, 500);
	assert.deepEqual(server.calls[0], { offset: 0, limit: fill.PLAYLIST_FIRST_PAGE });
	for (const call of server.calls.slice(1)) {
		assert.equal(call.limit, fill.PLAYLIST_FILL_PAGE);
	}
	// 1 first page + ceil((10042 - 30) / 500) fill pages.
	assert.equal(server.calls.length, 1 + Math.ceil((10042 - 30) / 500));
});

for (const n of [0, 1, 29, 30, 31, 529, 530, 531, 1031, 10042]) {
	test(`LIBM-134 every member appears exactly once, in order (n=${n})`, async () => {
		const { server, p } = await fillAll(n);
		assert.deepEqual(
			p.rows.map((r) => r.stable_id),
			server.members
		);
		assert.deepEqual(
			p.rows.map((r) => r.order),
			server.members.map((_, i) => i + 1)
		);
		assert.equal(p.load_progress, null);
		assert.equal(p.truncated, false);
	});
}
