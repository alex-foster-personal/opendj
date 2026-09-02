import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Browser pane contract (browser-surface unit). Regression lines:
// - if createPaneStore() defaults differ from the blank pane then broken
// - if beginLoad doesn't reset rows/selection/scroll then broken
// - if a stale completeLoad/failLoad mutates a newer load's pane then broken
// - if toggleSort same-key doesn't cycle asc → desc → clear then broken
// - if filterRows hide-broken doesn't compose with search then broken
// - if sortRows doesn't keep nulls last in BOTH directions then broken
// - if provider rows/total/truncated don't live-track sources then broken
// - if fetchWindow doesn't slice [start, start+count) or accepts negative args then broken
// - if completeLoad doesn't persist the playlist etag, or beginLoad doesn't
//   reset it, then the add-remove-reorder-tracks write path CASes against
//   a stale/wrong playlist -- broken
// - if a truncated playlist is write-enabled then a full-list replace can
//   discard unrendered members beyond the browser's fetch cap -- broken
// - if a decoded strip only reaches the pane whose selection triggered the
//   decode, and not a matching row's copy loaded in another pane, then broken
// - if a row's audience-ambiguous, listing-hydrated strip survives a fresh
//   audience-scoped /anlz decode for the same selected track then broken

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
		quality: null,
		play_count: 0,
		is_streaming: null,
		strip: null,
		vocals: { status: 'not_analyzed' },
		stems: { status: 'none' },
		rb_meta: null,
		revealed: false,
		match_context: null,
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

test('toggleSort: asc → desc → clear (natural order)', () => {
	const p = contract.createPaneStore();
	p.toggleSort('bpm');
	assert.equal(p.sort_key, 'bpm');
	assert.equal(p.sort_dir, 1);
	p.toggleSort('bpm');
	assert.equal(p.sort_dir, -1);
	p.toggleSort('bpm');
	assert.equal(p.sort_key, null);
	assert.equal(p.sort_dir, 1);
	p.toggleSort('title'); // switching keys starts ascending
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

test('filterRows: genre: strict token vs genre:~ loose substring', () => {
	const rows = [
		_row({ stable_id: 'exact', genre: 'House, Disco' }),
		_row({ stable_id: 'deep', genre: 'Deep House' }),
		_row({ stable_id: 'title-house', title: 'House Party', genre: 'Techno' }),
		_row({
			stable_id: 'meta',
			rb_meta: { artwork_available: false, genre: 'Tech House', is_streaming: false }
		})
	];
	assert.deepEqual(
		contract.filterRows(rows, 'genre:House', false).map((r) => r.stable_id),
		['exact']
	);
	assert.deepEqual(
		contract.filterRows(rows, 'genre:~House', false).map((r) => r.stable_id),
		['exact', 'deep', 'meta']
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

// ------------------------------------------- sticky panes + range select
// Regression lines:
// - if a fresh pane starts sticky then every playlist click opens a new tab
//   instead of loading in place -- broken
// - if beginLoad clears sticky then a locked tab silently unlocks the moment
//   it loads anything -- broken
// - if resolveNewTabIndex doesn't skip sticky panes, or doesn't return null
//   when all are locked, then a locked tab gets stolen -- broken
// - if shift-click range select doesn't span the visible order, or doesn't
//   fall back to a single select when either end is missing, then broken
// - if the two-argument select call sites change behaviour then broken
// - if decodePlaylistDrag accepts a foreign or malformed payload, or throws
//   instead of returning null, then a drag from another app crashes the
//   tab bar -- broken

test('a new pane is not sticky, and beginLoad leaves the lock alone', () => {
	const p = contract.createPaneStore();
	assert.equal(p.sticky, false);

	p.sticky = true;
	p.beginLoad('pl-1', 'Warmup');
	assert.equal(p.sticky, true, 'the lock belongs to the tab, not to its contents');
});

test('resolveNewTabIndex skips locked panes and reports all-locked as null', () => {
	const panes = [0, 1, 2, 3].map(() => contract.createPaneStore());
	assert.equal(contract.resolveNewTabIndex(panes), 0);

	panes[0].sticky = true;
	panes[1].sticky = true;
	assert.equal(contract.resolveNewTabIndex(panes), 2);

	for (const p of panes) p.sticky = true;
	assert.equal(contract.resolveNewTabIndex(panes), null);
});

test('range select spans the visible order in both directions', () => {
	const ordered = ['a', 'b', 'c', 'd', 'e'];
	const p = contract.createPaneStore();

	p.select('b', false);
	p.select('d', false, true, ordered);
	assert.deepEqual(p.selected_ids, ['b', 'c', 'd']);
	assert.equal(p.selected_id, 'd', 'the clicked row becomes the current row');

	// Dragging the span backwards yields the same set, not a reversed one.
	p.select('d', false);
	p.select('b', false, true, ordered);
	assert.deepEqual(p.selected_ids, ['b', 'c', 'd']);
	assert.equal(p.selected_id, 'b');
});

test('range select of a single row selects exactly that row', () => {
	const p = contract.createPaneStore();
	p.select('c', false);
	p.select('c', false, true, ['a', 'b', 'c']);
	assert.deepEqual(p.selected_ids, ['c']);
});

test('range select falls back to single select when an end is missing', () => {
	const ordered = ['a', 'b', 'c'];

	// No anchor at all: the first click in a pane cannot be a range.
	const fresh = contract.createPaneStore();
	fresh.select('b', false, true, ordered);
	assert.deepEqual(fresh.selected_ids, ['b']);
	assert.equal(fresh.selected_id, 'b');

	// Anchor was filtered or sorted out of the visible list.
	const filtered = contract.createPaneStore();
	filtered.select('zz-not-visible', false);
	filtered.select('c', false, true, ordered);
	assert.deepEqual(filtered.selected_ids, ['c']);

	// Empty visible order (caller passed nothing to span).
	const empty = contract.createPaneStore();
	empty.select('a', false);
	empty.select('b', false, true, []);
	assert.deepEqual(empty.selected_ids, ['b']);
});

test('extend and plain select keep their pre-range behaviour', () => {
	const p = contract.createPaneStore();
	p.select('a', false);
	assert.deepEqual(p.selected_ids, ['a']);

	p.select('b', true);
	assert.deepEqual(p.selected_ids, ['a', 'b']);

	p.select('a', true); // toggles back off
	assert.deepEqual(p.selected_ids, ['b']);

	p.select('c', false); // plain click collapses the selection
	assert.deepEqual(p.selected_ids, ['c']);
});

test('playlist drag payloads round-trip, and junk decodes to null', () => {
	const payload = {
		playlist_id: 'pl-7',
		name: 'Peak Time',
		track_count: 42,
		kind: 'playlist'
	};
	assert.deepEqual(
		contract.decodePlaylistDrag(contract.encodePlaylistDrag(payload)),
		payload
	);

	for (const junk of [
		'',
		'   ',
		'not json at all',
		'null',
		'[]',
		'"a string"',
		JSON.stringify({ playlist_id: '', name: 'n', track_count: 1, kind: 'playlist' }),
		JSON.stringify({ name: 'n', track_count: 1, kind: 'playlist' }),
		JSON.stringify({ playlist_id: 'p', track_count: 1, kind: 'playlist' }),
		JSON.stringify({ playlist_id: 'p', name: 'n', kind: 'playlist' }),
		JSON.stringify({ playlist_id: 'p', name: 'n', track_count: 1 }),
		JSON.stringify({ playlist_id: 'p', name: 'n', track_count: 1, kind: 'nope' })
	]) {
		assert.equal(contract.decodePlaylistDrag(junk), null, `should reject ${junk}`);
	}
});


// ------------------------------------------------------ strip propagation

test('a decoded strip reaches every pane holding a matching row, not only the selecting pane', () => {
	const stripA = { columns: 'aGVsbG8=', max: 200 };
	const paneOne = contract.createPaneStore();
	paneOne.rows = [_row({ stable_id: 'track-a', strip: null })];
	paneOne.selected_id = 'track-a';
	const paneTwo = contract.createPaneStore();
	paneTwo.rows = [_row({ stable_id: 'track-a', strip: null })];
	paneTwo.selected_id = 'track-b'; // a DIFFERENT selection, same track loaded as a row

	contract.applyDecodedStripAcrossPanes([paneOne, paneTwo], 'track-a', stripA);

	assert.deepEqual(paneOne.rows[0].strip, stripA);
	assert.deepEqual(
		paneTwo.rows[0].strip,
		stripA,
		'pane 2 never selected track-a, but its copy shares the same cache entry'
	);
});

test('a fresh audience-scoped decode overwrites a stale, sidecar-hydrated strip', () => {
	// The only other source of a strip on a locally-decoded track is listing
	// hydration's audience-ambiguous sidecar (TECH-DEBT.md,
	// discussion_r3907125683) - once the selected track's own /anlz returns
	// the audience-correct decode, it must win over that stale value
	// (discussion_r3909987026), not be silently preserved forever.
	const staleFromWrongAudience = { columns: 'c3RhbGU=', max: 30 };
	const freshCorrectDecode = { columns: 'ZnJlc2g=', max: 220 };
	const p = contract.createPaneStore();
	p.rows = [_row({ stable_id: 'track-a', strip: staleFromWrongAudience })];

	contract.applyDecodedStripAcrossPanes([p], 'track-a', freshCorrectDecode);

	assert.deepEqual(p.rows[0].strip, freshCorrectDecode);
});

test('applyDecodedStripAcrossPanes leaves rows for a different stable_id alone', () => {
	const strip = { columns: 'b3RoZXI=', max: 10 };
	const p = contract.createPaneStore();
	p.rows = [_row({ stable_id: 'track-unrelated', strip: null })];

	contract.applyDecodedStripAcrossPanes([p], 'track-a', strip);

	assert.equal(p.rows[0].strip, null);
});
