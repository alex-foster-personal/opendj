// requirement: USBPLAY-05, USBPLAY-09
/**
 * Play from USB: the stick library wire mapped onto browser rows and the
 * stick tree (src/lib/rb/usb-row-wire.ts).
 *
 * Fixtures are synthetic: the repo is public, so no title, path or id from a
 * real stick appears here.
 *
 * Regression lines:
 * - [if] a stick track row is not keyed by its usb- id [then] deck loads and
 *   selection address the wrong track
 * - [if] a removed stick's rows still read 'present' [then] a pulled stick
 *   looks loadable
 * - [if] a playlist's rows are not in the stick's entry order [then] the pane
 *   disagrees with rekordbox
 * - [if] the tree is not All tracks, then rekordbox order, then History
 *   [then] the stick browses differently from the CDJ
 * - [if] two sticks' pane ids can collide [then] their tracks mix
 * - [if] a malformed payload is accepted [then] a guessed row reaches a deck
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const UUID_A = 'AAAAAAAA-0000-4000-8000-00000000000A';
const UUID_B = 'BBBBBBBB-0000-4000-8000-00000000000B';

let wire;
before(async () => {
	wire = await loadTypeScriptModule('src/lib/rb/usb-row-wire.ts');
});

function track(uuid, pdbId, extra = {}) {
	return {
		id: `usb-${uuid}-${pdbId}`,
		pdb_id: pdbId,
		title: `Synthetic ${pdbId}`,
		artist: 'Fixture Artist',
		album: null,
		genre: 'House',
		key: '8A',
		bpm: 124.5,
		duration_s: 301.456,
		rating: 3,
		file_path: `/Contents/fixture/${pdbId}.mp3`,
		has_analysis: true,
		has_artwork: false,
		date_added: '2026-01-02',
		...extra
	};
}

function playlist(n, extra = {}) {
	return {
		id: `pl-${n}`,
		pdb_id: n,
		name: `List ${n}`,
		parent_id: null,
		is_folder: false,
		sort_order: n,
		track_ids: [],
		...extra
	};
}

function library(uuid, { tracks = [], playlists = [], history = [] } = {}) {
	return {
		volume_id: `vol:${uuid}`,
		volume_uuid: uuid,
		name: 'FIXTURE STICK ',
		mount_path: '/Volumes/FIXTURE STICK ',
		tracks,
		playlists,
		history,
		counts: {
			tracks: tracks.length,
			playlists: playlists.length,
			playlist_entries: playlists.reduce((n, p) => n + p.track_ids.length, 0),
			history_playlists: history.length
		},
		read_ms: 12.5
	};
}

function errorOf(fn) {
	try {
		fn();
	} catch (exc) {
		assert.equal(exc.name, 'UsbLibraryError', `expected a typed UsbLibraryError, got ${exc}`);
		return exc;
	}
	assert.fail('expected a UsbLibraryError, nothing was thrown');
}

function codeOf(fn) {
	return errorOf(fn).code;
}

describe('rowFromUsbTrack', () => {
	it('keys the row by the usb- id and types every numeric field', () => {
		const row = wire.rowFromUsbTrack(track(UUID_A, 36), { volume_uuid: UUID_A, present: true }, 1);
		assert.equal(row.stable_id, `usb-${UUID_A}-36`);
		assert.equal(row.order, 1);
		assert.equal(row.bpm, 124.5);
		assert.equal(row.duration_ms, 301456);
		assert.ok(Number.isInteger(row.duration_ms), 'duration_ms must be whole milliseconds');
		assert.equal(row.rating, 3);
		assert.equal(row.play_count, 0);
		assert.equal(row.key, '8A');
		assert.equal(row.genre, 'House');
		assert.equal(row.genre_reason, null);
	});

	it('is present while mounted and awaiting_volume once removed', () => {
		const mounted = wire.rowFromUsbTrack(track(UUID_A, 1), { volume_uuid: UUID_A, present: true }, 1);
		assert.equal(mounted.file_exists, true);
		assert.equal(mounted.file_availability, 'present');
		const removed = wire.rowFromUsbTrack(track(UUID_A, 1), { volume_uuid: UUID_A, present: false }, 1);
		assert.equal(removed.file_exists, false);
		assert.equal(removed.file_availability, 'awaiting_volume');
	});

	it('never borrows library data: no rb mapping, strip, stems or energy', () => {
		const row = wire.rowFromUsbTrack(track(UUID_A, 2), { volume_uuid: UUID_A, present: true }, 1);
		assert.equal(row.has_rb_mapping, false);
		assert.equal(row.rb_meta, null);
		assert.equal(row.strip, null);
		assert.equal(row.energy, null);
		assert.match(row.energy_reason, /stick/);
		assert.deepEqual(row.stems, { status: 'none' });
		assert.equal(row.is_streaming, false);
	});

	it('keeps unknown values null instead of inventing them', () => {
		const row = wire.rowFromUsbTrack(
			track(UUID_A, 3, { bpm: null, duration_s: null, genre: null, key: null, artist: null }),
			{ volume_uuid: UUID_A, present: true },
			1
		);
		assert.equal(row.bpm, null);
		assert.equal(row.duration_ms, null);
		assert.equal(row.key, null);
		assert.equal(row.artist, null);
		assert.equal(row.genre, null);
		assert.match(row.genre_reason, /no genre/);
	});

	it('refuses a track id that belongs to another stick', () => {
		const code = codeOf(() =>
			wire.rowFromUsbTrack(track(UUID_B, 4), { volume_uuid: UUID_A, present: true }, 1)
		);
		assert.equal(code, 'USB_TRACK_ID_INVALID');
	});
});

describe('usbRowsForNode', () => {
	const lib = library(UUID_A, {
		tracks: [track(UUID_A, 1), track(UUID_A, 2), track(UUID_A, 3)],
		playlists: [
			playlist(10, { track_ids: [`usb-${UUID_A}-3`, `usb-${UUID_A}-1`, `usb-${UUID_A}-3`] }),
			playlist(11, { is_folder: true })
		],
		history: [{ id: 'hist-1', name: 'HISTORY 001', track_ids: [`usb-${UUID_A}-2`] }]
	});

	it('keeps the stick entry order, duplicates included, with 1-based slots', () => {
		const rows = wire.usbRowsForNode(lib, 'pl-10', true);
		assert.deepEqual(
			rows.map((r) => [r.stable_id, r.order]),
			[
				[`usb-${UUID_A}-3`, 1],
				[`usb-${UUID_A}-1`, 2],
				[`usb-${UUID_A}-3`, 3]
			]
		);
	});

	it('serves All tracks and History lists', () => {
		assert.equal(wire.usbRowsForNode(lib, 'all', true).length, 3);
		assert.deepEqual(
			wire.usbRowsForNode(lib, 'hist-1', true).map((r) => r.stable_id),
			[`usb-${UUID_A}-2`]
		);
	});

	it('fails typed for an unknown node, a folder, or a dangling track id', () => {
		assert.equal(codeOf(() => wire.usbRowsForNode(lib, 'pl-99', true)), 'USB_NODE_NOT_FOUND');
		assert.equal(codeOf(() => wire.usbRowsForNode(lib, 'pl-11', true)), 'USB_NODE_NOT_FOUND');
		const dangling = library(UUID_A, {
			tracks: [track(UUID_A, 1)],
			playlists: [playlist(12, { track_ids: [`usb-${UUID_A}-404`] })]
		});
		assert.equal(codeOf(() => wire.usbRowsForNode(dangling, 'pl-12', true)), 'USB_TRACK_NOT_FOUND');
	});

	it('withUsbPresence grays rows in place, keeping ids and order', () => {
		const rows = wire.usbRowsForNode(lib, 'pl-10', true);
		const grayed = wire.withUsbPresence(rows, false);
		assert.deepEqual(
			grayed.map((r) => [r.stable_id, r.order, r.file_exists, r.file_availability]),
			rows.map((r) => [r.stable_id, r.order, false, 'awaiting_volume'])
		);
		const back = wire.withUsbPresence(grayed, true);
		assert.ok(back.every((r) => r.file_exists === true && r.file_availability === 'present'));
	});

	it('titles a pane with the stick name first', () => {
		assert.equal(wire.usbNodeTitle(lib, 'pl-10'), 'FIXTURE STICK / List 10');
		assert.equal(wire.usbNodeTitle(lib, 'all'), 'FIXTURE STICK / All tracks');
	});
});

describe('buildUsbTree', () => {
	function names(nodes) {
		return nodes.map((n) => n.name);
	}

	it('puts All tracks first, then rekordbox sort_order, then History', () => {
		const lib = library(UUID_A, {
			tracks: [track(UUID_A, 1), track(UUID_A, 2)],
			// Input order and pdb_id order both disagree with sort_order on purpose.
			playlists: [
				playlist(1, { name: 'Third', sort_order: 3 }),
				playlist(2, { name: 'First', sort_order: 1 }),
				playlist(3, { name: 'Second', sort_order: 2 })
			],
			history: [{ id: 'hist-1', name: 'HISTORY 001', track_ids: [] }]
		});
		const tree = wire.buildUsbTree(lib);
		assert.deepEqual(names(tree), ['All tracks', 'First', 'Second', 'Third', 'History']);
		assert.equal(tree[0].kind, 'usb');
		assert.equal(tree[0].playlist_id, `usbpl:${UUID_A}:all`);
		assert.equal(tree[0].track_count, 2);
		const history = tree[4];
		assert.equal(history.kind, 'folder');
		assert.deepEqual(names(history.children), ['HISTORY 001']);
		assert.equal(history.children[0].playlist_id, `usbpl:${UUID_A}:hist-1`);
	});

	it('nests playlists under their folders, each level in sort_order', () => {
		const lib = library(UUID_A, {
			playlists: [
				playlist(5, { name: 'Folder', is_folder: true, sort_order: 1 }),
				playlist(6, { name: 'Inner B', parent_id: 'pl-5', sort_order: 2, track_ids: ['x', 'y'] }),
				playlist(7, { name: 'Inner A', parent_id: 'pl-5', sort_order: 1 }),
				playlist(8, { name: 'Top', sort_order: 2 })
			]
		});
		const tree = wire.buildUsbTree(lib);
		assert.deepEqual(names(tree), ['All tracks', 'Folder', 'Top']);
		const folder = tree[1];
		assert.equal(folder.kind, 'folder');
		assert.equal(folder.track_count, 2, 'a folder counts its lists');
		assert.deepEqual(names(folder.children), ['Inner A', 'Inner B']);
		assert.equal(folder.children[1].track_count, 2);
		assert.equal(folder.children[1].kind, 'usb');
	});

	it('breaks sort_order ties by pdb id, so the order is deterministic', () => {
		const lib = library(UUID_A, {
			playlists: [
				playlist(9, { name: 'Nine', sort_order: 0 }),
				playlist(4, { name: 'Four', sort_order: 0 })
			]
		});
		assert.deepEqual(names(wire.buildUsbTree(lib)), ['All tracks', 'Four', 'Nine']);
	});

	it('has no History folder when the stick has no history', () => {
		const tree = wire.buildUsbTree(library(UUID_A, { playlists: [playlist(1)] }));
		assert.deepEqual(names(tree), ['All tracks', 'List 1']);
	});

	it('fails typed for a dangling parent, a non-folder parent, or a cycle', () => {
		// Each case names its own cause: a list nested under a list would
		// otherwise only surface as a "cycle", which sends the reader looking
		// for the wrong defect in the export.
		const dangling = library(UUID_A, { playlists: [playlist(1, { parent_id: 'pl-99' })] });
		const danglingError = errorOf(() => wire.buildUsbTree(dangling));
		assert.equal(danglingError.code, 'USB_TREE_INVALID');
		assert.match(danglingError.message, /pl-99, which is not a folder/);
		const underList = library(UUID_A, {
			playlists: [playlist(1), playlist(2, { parent_id: 'pl-1' })]
		});
		const underListError = errorOf(() => wire.buildUsbTree(underList));
		assert.equal(underListError.code, 'USB_TREE_INVALID');
		assert.match(underListError.message, /pl-2 names parent pl-1, which is not a folder/);
		const cycle = library(UUID_A, {
			playlists: [
				playlist(1, { is_folder: true, parent_id: 'pl-2' }),
				playlist(2, { is_folder: true, parent_id: 'pl-1' })
			]
		});
		const cycleError = errorOf(() => wire.buildUsbTree(cycle));
		assert.equal(cycleError.code, 'USB_TREE_INVALID');
		assert.match(cycleError.message, /cycle/);
	});

	it('gives two sticks disjoint pane ids, so their trees never mix', () => {
		const idsOf = (nodes) => nodes.flatMap((n) => [n.playlist_id, ...idsOf(n.children)]);
		const a = idsOf(wire.buildUsbTree(library(UUID_A, { playlists: [playlist(1)] })));
		const b = idsOf(wire.buildUsbTree(library(UUID_B, { playlists: [playlist(1)] })));
		assert.equal(a.filter((id) => b.includes(id)).length, 0, `shared ids: ${a.filter((id) => b.includes(id))}`);
		assert.ok(a.every((id) => wire.parseUsbPaneId(id)?.volumeUuid === UUID_A));
	});
});

describe('pane ids', () => {
	it('round-trips a stick pane id', () => {
		const id = wire.usbPaneId(UUID_A, 'pl-3');
		assert.equal(id, `usbpl:${UUID_A}:pl-3`);
		assert.deepEqual(wire.parseUsbPaneId(id), { volumeUuid: UUID_A, nodeKey: 'pl-3' });
	});

	it('never reads a library playlist id or a stick TRACK id as a stick pane', () => {
		assert.equal(wire.parseUsbPaneId('pl-3'), null);
		assert.equal(wire.parseUsbPaneId('taglist:abc'), null);
		assert.equal(wire.parseUsbPaneId(`usb-${UUID_A}-36`), null);
		assert.equal(wire.parseUsbPaneId('usbpl::all'), null);
		assert.equal(wire.parseUsbPaneId(`usbpl:${UUID_A}:`), null);
	});
});

describe('parseUsbLibraryWire', () => {
	const good = () =>
		library(UUID_A, {
			tracks: [track(UUID_A, 1)],
			playlists: [playlist(1, { track_ids: [`usb-${UUID_A}-1`] })],
			history: [{ id: 'hist-1', name: 'HISTORY 001', track_ids: [] }]
		});

	it('accepts the route contract and returns it field for field', () => {
		const raw = good();
		assert.deepEqual(wire.parseUsbLibraryWire(raw), raw);
	});

	for (const [label, mutate] of [
		['a string bpm', (raw) => (raw.tracks[0].bpm = '124')],
		['a rating above 5', (raw) => (raw.tracks[0].rating = 6)],
		['a fractional rating', (raw) => (raw.tracks[0].rating = 2.5)],
		['a null title', (raw) => (raw.tracks[0].title = null)],
		['a missing track_ids', (raw) => delete raw.playlists[0].track_ids],
		['a non-boolean is_folder', (raw) => (raw.playlists[0].is_folder = 0)],
		['a missing counts object', (raw) => delete raw.counts],
		['a NaN read_ms', (raw) => (raw.read_ms = Number.NaN)]
	]) {
		it(`rejects ${label} as USB_LIBRARY_MALFORMED`, () => {
			const raw = good();
			mutate(raw);
			assert.equal(codeOf(() => wire.parseUsbLibraryWire(raw)), 'USB_LIBRARY_MALFORMED');
		});
	}
});
