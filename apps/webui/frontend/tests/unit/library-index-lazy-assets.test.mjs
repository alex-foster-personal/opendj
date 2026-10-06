// LIBM-172: library index rows carry no preview strip, vocal regions or cover
// verdict; TrackTable asks /library/row-assets for the rows in view only, and the
// boot defers its slow side requests in the boot scheduler queue until the index has landed.
//
// Regression one-liners:
//   - if the asset window asks for rows outside view plus margin then broken (mutation control)
//   - if any sort, filter or search orders index rows differently from full rows then broken
//   - if cover images skip the boot scheduler queue, or are not released when it drains, then broken
//   - if enrich summary, lyrics-cached-ids or tree prefetch bypass the boot scheduler then broken
//   - if a row-assets answer does not fill stems, vocals and cover on the asked row then broken
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const read = (p) => readFileSync(fileURLToPath(new URL(`../../${p}`, import.meta.url)), 'utf8');

let fill;
let wire;
let contract;
before(async () => {
	fill = await loadTypeScriptModule('src/lib/rb/preview-strip-fill.ts');
	wire = await loadTypeScriptModule('src/lib/components/rb/browser/browser-row-wire.ts');
	contract = await loadTypeScriptModule('src/lib/components/rb/browser/pane-contract.svelte.ts');
});

test('the asset window asks only for rows in view plus margin', () => {
	const rows = Array.from({ length: 10_000 }, (_, i) => ({ stable_id: `t${i}`, strip: null }));
	rows[5_010].strip = 'held';
	const ids = fill.stripLessIdsNear(rows, 5_000, 5_030, 30, (id) => id === 't5020');
	assert.equal(ids.length, 90 - 2);
	assert.equal(ids[0], 't4970');
	assert.equal(ids.at(-1), 't5059');
	assert.ok(!ids.includes('t5010') && !ids.includes('t5020'), 'rows with a strip are not asked for');
	// Mutation control: a window that ignored the view would ask for all 10,000.
	const everything = fill.stripLessIdsNear(rows, 0, rows.length, 0, () => false);
	assert.equal(everything.length, 9_999);
	assert.ok(ids.length < everything.length / 100);
});

const STRIP = Buffer.from(new Uint8Array(360).fill(5)).toString('base64');

function listWire(i) {
	return {
		stable_id: `s${String(i).padStart(3, '0')}`,
		title: ['Alpha', 'beta', 'Gamma', null][i % 4],
		artist: `Artist ${(i * 7) % 5}`,
		album: null,
		key: ['8A', '3B', null][i % 3],
		bpm: [124, 128, null, 98][i % 4],
		rating: i % 6,
		notes: i % 2 === 0 ? `note ${i}` : null,
		duration_ms: 180_000 + i * 1_000,
		genre: ['House', null, 'Techno'][i % 3],
		energy: [3, null, 7][i % 3],
		energy_source: null,
		energy_reason: 'no Mixed In Key value',
		file_availability: ['present', 'absent', 'AVAILABILITY_PENDING'][i % 3],
		file_exists: [true, false, null][i % 3],
		file_path: `/m/${i}.mp3`,
		is_streaming: false,
		has_remote_copy: false,
		cloud_transfer: null,
		has_rb_mapping: i % 2 === 0,
		quality: null,
		play_count: (i * 3) % 4,
		preview_b64: i % 5 === 0 ? null : STRIP,
		preview_max: i % 5 === 0 ? null : 5,
		vocals: i % 4 === 0 ? { status: 'no_vocals', fps: 10, regions: [] } : { status: 'not_analyzed' },
		stems: { status: 'none' },
		artwork_available: i % 3 === 0,
		artwork_status: i % 3 === 0 ? 'ok' : 'no_image_path',
		lyrics: null,
		grid_quality: null,
		is_remix: i % 4 === 1,
		is_radio_edit: false
	};
}

test('no sort, filter or search reads the deferred fields', () => {
	const DEFERRED = ['preview_b64', 'preview_max', 'vocals', 'artwork_available', 'artwork_status', 'stems'];
	const full = Array.from({ length: 40 }, (_, i) => wire.rowFromListWire(listWire(i), i + 1));
	const index = Array.from({ length: 40 }, (_, i) => {
		const item = listWire(i);
		for (const k of DEFERRED) delete item[k];
		return wire.rowFromIndexWire(item, i + 1);
	});
	assert.ok(
		index.every((r) => r.strip === null && r.artwork_available === null && r.stems === null),
		'index rows really lack them'
	);
	const ids = (rows) => rows.map((r) => r.stable_id).join(',');
	const keys = ['order', 'plays', 'title', 'artist', 'key', 'bpm', 'rating', 'comments', 'time', 'energy', 'genre', 'lyrics', 'grid', 'autoplay'];
	for (const key of keys) {
		for (const dir of [1, -1]) {
			assert.equal(ids(contract.sortRows(index, key, dir)), ids(contract.sortRows(full, key, dir)), `sort ${key} ${dir}`);
		}
	}
	for (const query of ['', 'alpha', 'artist 3', '8a', '124', 'note 4', 'house']) {
		for (const hideBroken of [false, true]) {
			assert.equal(
				ids(contract.filterRows(index, query, hideBroken)),
				ids(contract.filterRows(full, query, hideBroken)),
				`filter "${query}" hideBroken=${hideBroken}`
			);
		}
	}
	assert.equal(ids(index.filter(contract.rowHasVocalLyrics)), ids(full.filter(contract.rowHasVocalLyrics)));
});

test('cover images wait in the boot scheduler queue and are released when it drains', () => {
	const queue = [];
	const held = { defer: (label, task) => queue.push({ label, task }) };
	let released = 0;
	assert.equal(fill.holdArtworkForBoot(() => (released += 1), held), false, 'held while the queue is closed');
	assert.deepEqual(queue.map((q) => q.label), ['track-table:artwork']);
	assert.equal(released, 0);
	queue[0].task();
	assert.equal(released, 1, 'released once when the queue drains');

	const open = { defer: (_label, task) => task() };
	let late = 0;
	assert.equal(fill.holdArtworkForBoot(() => (late += 1), open), true, 'loads at once after boot');
	assert.equal(late, 0, 'and does not also call release');
});

test('the slow boot side requests go through the boot scheduler', () => {
	assert.match(read('src/lib/components/rb/EnrichCard.svelte'), /bootScheduler\.defer\('enrich-card:load', \(\) => void load\(\)\)/);
	assert.match(
		read('src/lib/components/rb/wave/lyrics-cached-ids.svelte.ts'),
		/bootScheduler\.defer\('lyrics-cached-ids:load', \(\) => \{\s*void loadLyricsCachedIds\(\)/
	);
	assert.match(
		read('src/lib/components/rb/BrowserPanel.svelte'),
		/bootScheduler\.defer\('browser-panel:playlist-tree-intent', \(\) => prefetchPlaylistTreeIntent\(ids\)\)/
	);
	const table = read('src/lib/components/rb/browser/TrackTable.svelte');
	assert.match(table, /artworkReleased &&\s*artworkAvailable === true/);
	assert.match(table, /holdArtworkForBoot\(\(\) => \(artworkReleased = true\)\)/);
});

test('a row-assets answer fills stems, vocals and cover for the asked rows only', async () => {
	const fillModule = await loadTypeScriptModule('src/lib/rb/row-assets-fill.ts', {
		viteApiBase: 'https://rows.example.test'
	});
	const originalFetch = globalThis.fetch;
	const asked = [];
	globalThis.fetch = async (input) => {
		const request = input instanceof Request ? input : new Request(input);
		asked.push({ url: request.url, body: await request.json() });
		return Response.json({
			assets: {
				t1: {
					preview_b64: STRIP,
					preview_max: 5,
					vocals: { status: 'no_vocals', fps: 10, regions: [] },
					artwork_available: true,
					artwork_status: 'ok',
					stems: { status: 'invalid', error: 'bad bundle' }
				}
			},
			pending: []
		});
	};
	try {
		const row = (id) => ({
			stable_id: id,
			vocals: { status: 'not_analyzed' },
			artwork_available: null,
			artwork_status: 'unresolved',
			stems: null
		});
		const rows = [row('t1'), row('t2')];
		const out = await fillModule.fetchAndApplyRowAssets(['t1'], rows);
		assert.equal(asked.length, 1);
		assert.equal(asked[0].url, 'https://rows.example.test/api/v1/library/row-assets');
		assert.deepEqual(asked[0].body, { ids: ['t1'] });
		assert.deepEqual(rows[0].stems, { status: 'invalid', error: 'bad bundle' });
		assert.equal(rows[0].vocals.status, 'no_vocals');
		assert.equal(rows[0].artwork_available, true);
		assert.deepEqual(rows[1], row('t2'), 'a row not asked for is untouched');
		assert.deepEqual(out, { strips: { t1: { preview_b64: STRIP, preview_max: 5 } }, pending: [] });
	} finally {
		globalThis.fetch = originalFetch;
	}
});
