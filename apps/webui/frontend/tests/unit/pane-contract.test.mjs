import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Browser pane contract (browser-surface unit). Regression lines:
// - if createPaneStore() defaults differ from the blank pane then broken
// - if beginLoad doesn't reset rows/selection/scroll then broken
// - if a stale completeLoad/failLoad mutates a newer load's pane then broken
// - if toggleSort same-key doesn't flip / new-key doesn't reset asc then broken
// - if filterRows hide-broken doesn't compose with search then broken
// - if sortRows doesn't keep nulls last in BOTH directions then broken
// - if provider rows/total/truncated don't live-track sources then broken
// - if fetchWindow doesn't slice [start, start+count) or accepts negative args then broken
// - if completeLoad doesn't persist the playlist etag, or beginLoad doesn't
//   reset it, then the add-remove-reorder-tracks write path CASes against
//   a stale/wrong playlist -- broken
// - if a truncated playlist is write-enabled then a full-list replace can
//   discard unrendered members beyond the browser's fetch cap -- broken

let contract;

before(async () => {
	contract = await loadTypeScriptModule(
		'src/lib/components/rb/browser/pane-contract.svelte.ts'
	);
});

function _row(overrides = {}) {
	return {
		stable_id: 'sid-default',
		order: 1,
		title: null,
		artist: null,
		key: null,
		bpm: null,
		rating: null,
		etag: '',
		comments: null,
		duration_ms: null,
		genre: null,
		file_exists: true,
		is_streaming: null,
		strip: null,
		rb_meta: null,
		revealed: false,
		...overrides
	};
}

// ------------------------------------------------------------ pane store

test('createPaneStore defaults match the blank pane', () => {
	const p = contract.createPaneStore();
	assert.equal(p.playlist_id, null);
	assert.equal(p.title, 'blank list');
	assert.deepEqual(p.rows, []);
	assert.equal(p.loading, false);
	assert.equal(p.error, null);
	assert.equal(p.search, '');
	assert.equal(p.selected_id, null);
	assert.equal(p.sort_key, null);
	assert.equal(p.sort_dir, 1);
	assert.equal(p.truncated, false);
	assert.equal(p.scroll_top, 0);
	assert.equal(p.etag, '');
});

test('completeLoad persists the playlist etag; beginLoad resets it for the next load', () => {
	const p = contract.createPaneStore();
	const seq = p.beginLoad('pl-1', 'Warmup');
	assert.equal(p.etag, '');
	assert.equal(p.completeLoad(seq, [_row()], false, '"abc123"'), true);
	assert.equal(p.etag, '"abc123"');

	// A fresh load (e.g. switching playlists) must not leak the old etag.
	p.beginLoad('pl-2', 'Peak Hour');
	assert.equal(p.etag, '');

	// Omitting etag (the All Tracks / blank-pane load path) defaults to ''.
	const allTracksSeq = p.beginLoad('all', 'All Tracks');
	assert.equal(p.completeLoad(allTracksSeq, [_row()], false), true);
	assert.equal(p.etag, '');
});

test('canMutatePlaylist rejects blank, all-tracks, unloaded, truncated, and global-search panes', () => {
	assert.equal(contract.canMutatePlaylist({ playlist_id: null, etag: '"v1"', truncated: false, whole_collection: false }), false);
	assert.equal(contract.canMutatePlaylist({ playlist_id: 'all', etag: '"v1"', truncated: false, whole_collection: false }), false);
	assert.equal(contract.canMutatePlaylist({ playlist_id: 'pl-1', etag: '', truncated: false, whole_collection: false }), false);
	assert.equal(contract.canMutatePlaylist({ playlist_id: 'pl-1', etag: '"v1"', truncated: true, whole_collection: false }), false);
	assert.equal(contract.canMutatePlaylist({ playlist_id: 'pl-1', etag: '"v1"', truncated: false, whole_collection: true }), false);
	assert.equal(contract.canMutatePlaylist({ playlist_id: 'pl-1', etag: '"v1"', truncated: false, whole_collection: false }), true);
});

test('beginLoad resets the pane and completeLoad publishes rows for the current token', () => {
	const p = contract.createPaneStore();
	p.select('sid-old');
	p.rememberScroll(240);
	p.truncated = true;
	p.error = 'stale error';

	const seq = p.beginLoad('pl-1', 'Warmup');
	assert.equal(p.playlist_id, 'pl-1');
	assert.equal(p.title, 'Warmup');
	assert.deepEqual(p.rows, []);
	assert.equal(p.loading, true);
	assert.equal(p.error, null);
	assert.equal(p.selected_id, null);
	assert.equal(p.truncated, false);
	assert.equal(p.scroll_top, 0);
	assert.equal(p.isCurrentLoad(seq), true);

	const rows = [_row({ stable_id: 'a' }), _row({ stable_id: 'b', order: 2 })];
	assert.equal(p.completeLoad(seq, rows, true), true);
	assert.equal(p.rows.length, 2);
	assert.equal(p.truncated, true);
	assert.equal(p.loading, false);
});

test('stale completeLoad and failLoad are full no-ops once a newer load begins', () => {
	const p = contract.createPaneStore();
	const stale = p.beginLoad('pl-1', 'First');
	const fresh = p.beginLoad('pl-2', 'Second');

	assert.equal(p.isCurrentLoad(stale), false);
	assert.equal(p.completeLoad(stale, [_row()], true), false);
	assert.deepEqual(p.rows, []); // the newer load still owns the pane
	assert.equal(p.loading, true); // stale must NOT clear the fresh spinner
	assert.equal(p.failLoad(stale, 'boom'), false);
	assert.equal(p.error, null);

	assert.equal(p.completeLoad(fresh, [_row({ stable_id: 'z' })], false), true);
	assert.equal(p.rows[0].stable_id, 'z');
	assert.equal(p.loading, false);
});

test('failLoad records the error and clears loading for the current load', () => {
	const p = contract.createPaneStore();
	const seq = p.beginLoad('pl-1', 'Broken');
	assert.equal(p.failLoad(seq, 'Error: 500'), true);
	assert.equal(p.error, 'Error: 500');
	assert.equal(p.loading, false);
});

test('toggleSort: new key starts ascending, same key flips direction', () => {
	const p = contract.createPaneStore();
	p.toggleSort('bpm');
	assert.equal(p.sort_key, 'bpm');
	assert.equal(p.sort_dir, 1);
	p.toggleSort('bpm');
	assert.equal(p.sort_dir, -1);
	p.toggleSort('bpm');
	assert.equal(p.sort_dir, 1);
	p.toggleSort('title'); // switching keys resets to ascending
	assert.equal(p.sort_key, 'title');
	assert.equal(p.sort_dir, 1);
});

test('select / setSearch / rememberScroll write the pane cursor state', () => {
	const p = contract.createPaneStore();
	p.select('sid-9');
	p.setSearch('acid');
	p.rememberScroll(512);
	assert.equal(p.selected_id, 'sid-9');
	assert.equal(p.search, 'acid');
	assert.equal(p.scroll_top, 512);
});

// ------------------------------------------------------ filter pipeline

test('filterRows: hide-broken applies before search and both compose', () => {
	const rows = [
		_row({ stable_id: 'a', title: 'Acid Rain', file_exists: true }),
		_row({ stable_id: 'b', title: 'Acid Storm', file_exists: false }),
		_row({ stable_id: 'c', title: 'Deep Blue', file_exists: true })
	];
	assert.deepEqual(
		contract.filterRows(rows, '', true).map((r) => r.stable_id),
		['a', 'c']
	);
	assert.deepEqual(
		contract.filterRows(rows, 'acid', false).map((r) => r.stable_id),
		['a', 'b']
	);
	assert.deepEqual(
		contract.filterRows(rows, 'acid', true).map((r) => r.stable_id),
		['a']
	);
	assert.equal(contract.filterRows(rows, '', false).length, 3);
});

test('filterRows: case-insensitive match over title/artist/comments/key and rb_meta genre fallback', () => {
	const rows = [
		_row({ stable_id: 'artist', artist: 'DJ TECHNO' }),
		_row({ stable_id: 'comments', comments: 'closing techno set' }),
		_row({ stable_id: 'key', key: '8A' }),
		_row({ stable_id: 'inline-genre', genre: 'Hard Techno' }),
		_row({
			stable_id: 'meta-genre',
			rb_meta: { artwork_available: false, genre: 'Melodic Techno', is_streaming: false }
		}),
		_row({ stable_id: 'miss', title: 'Ambient Dawn' })
	];
	assert.deepEqual(
		contract.filterRows(rows, 'TeChNo', false).map((r) => r.stable_id),
		['artist', 'comments', 'inline-genre', 'meta-genre']
	);
	assert.deepEqual(
		contract.filterRows(rows, '8a', false).map((r) => r.stable_id),
		['key']
	);
});

// -------------------------------------------------------- sort pipeline

test('sortRows: numeric asc/desc with nulls last in BOTH directions, input not mutated', () => {
	const rows = [
		_row({ stable_id: 'null-bpm', bpm: null }),
		_row({ stable_id: 'fast', bpm: 140 }),
		_row({ stable_id: 'slow', bpm: 122 })
	];
	const asc = contract.sortRows(rows, 'bpm', 1);
	assert.deepEqual(
		asc.map((r) => r.stable_id),
		['slow', 'fast', 'null-bpm']
	);
	const desc = contract.sortRows(rows, 'bpm', -1);
	assert.deepEqual(
		desc.map((r) => r.stable_id),
		['fast', 'slow', 'null-bpm'] // nulls stay last even descending
	);
	// membership order untouched (sortRows returns a copy)
	assert.deepEqual(
		rows.map((r) => r.stable_id),
		['null-bpm', 'fast', 'slow']
	);
});

test('sortRows: string keys use locale compare; null key returns rows unsorted', () => {
	const rows = [
		_row({ stable_id: 'b', title: 'Beta' }),
		_row({ stable_id: 'a', title: 'alpha' }),
		_row({ stable_id: 'n', title: null })
	];
	assert.deepEqual(
		contract.sortRows(rows, 'title', 1).map((r) => r.stable_id),
		['a', 'b', 'n']
	);
	assert.equal(contract.sortRows(rows, null, 1), rows);
});

test('sortValue maps time to duration_ms and genre to the rb_meta fallback', () => {
	const row = _row({
		duration_ms: 61000,
		genre: null,
		rb_meta: { artwork_available: false, genre: 'House', is_streaming: false }
	});
	assert.equal(contract.sortValue(row, 'time'), 61000);
	assert.equal(contract.sortValue(row, 'genre'), 'House');
	assert.equal(contract.sortValue(_row({ order: 7 }), 'order'), 7);
});

test('visibleRowsOf applies FR-1 filter, then search, then sort', () => {
	const p = contract.createPaneStore();
	const seq = p.beginLoad('pl-1', 'Mix');
	p.completeLoad(
		seq,
		[
			_row({ stable_id: 'broken', title: 'Techno One', bpm: 150, file_exists: false }),
			_row({ stable_id: 'fast', title: 'Techno Two', bpm: 145 }),
			_row({ stable_id: 'slow', title: 'Techno Three', bpm: 120 }),
			_row({ stable_id: 'other', title: 'Ambient', bpm: 90 })
		],
		false
	);
	p.setSearch('techno');
	p.toggleSort('bpm');
	assert.deepEqual(
		contract.visibleRowsOf(p, true).map((r) => r.stable_id),
		['slow', 'fast']
	);
	assert.deepEqual(
		contract.visibleRowsOf(p, false).map((r) => r.stable_id),
		['slow', 'fast', 'broken']
	);
});

// -------------------------------------------------------- row provider

test('makeClientRowProvider live-tracks its source closures', () => {
	let rows = [_row({ stable_id: 'a' })];
	let truncated = false;
	const provider = contract.makeClientRowProvider(
		() => rows,
		() => truncated
	);
	assert.equal(provider.rows.length, 1);
	assert.equal(provider.total, 1);
	assert.equal(provider.truncated, false);

	rows = [_row({ stable_id: 'a' }), _row({ stable_id: 'b' }), _row({ stable_id: 'c' })];
	truncated = true;
	assert.equal(provider.rows.length, 3); // getters re-read, no snapshot
	assert.equal(provider.total, 3);
	assert.equal(provider.truncated, true);
});

test('fetchWindow resolves rows[start, start+count) and clamps at the end', async () => {
	const rows = ['a', 'b', 'c', 'd', 'e'].map((id, i) => _row({ stable_id: id, order: i + 1 }));
	const provider = contract.makeClientRowProvider(
		() => rows,
		() => false
	);
	assert.deepEqual(
		(await provider.fetchWindow(1, 2)).map((r) => r.stable_id),
		['b', 'c']
	);
	assert.deepEqual(
		(await provider.fetchWindow(4, 10)).map((r) => r.stable_id),
		['e'] // window past the end clamps, no padding
	);
	assert.deepEqual(await provider.fetchWindow(9, 3), []);
});

test('fetchWindow fails fast on negative window args', () => {
	const provider = contract.makeClientRowProvider(
		() => [],
		() => false
	);
	assert.throws(() => provider.fetchWindow(-1, 5), /negative window/);
	assert.throws(() => provider.fetchWindow(0, -2), /negative window/);
});
