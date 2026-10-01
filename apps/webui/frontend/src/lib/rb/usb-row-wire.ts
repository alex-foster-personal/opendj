/**
 * Play from USB (USBPLAY-05, USBPLAY-09): the stick library wire, mapped onto
 * the browser's own row and tree contracts.
 *
 * Source: GET /api/v1/usb/volumes/{volume_id}/library, which reads the stick's
 * rekordbox `export.pdb` and never writes to it. Nothing here touches the main
 * library: stick rows are built from honest "unknown" values (no strip, no
 * stems, no rekordbox mapping) and never borrow a library track's data.
 *
 * Pure and runeless so node tests load it directly. Every malformed payload
 * fails loudly: a stick is untrusted removable media, and a guessed row would
 * load the wrong audio onto a deck.
 *
 * Pane ids: `usbpl:<VolumeUUID>:<node>` where node is `all`, `history`, a
 * playlist or folder id (`pl-<n>`) or a history id (`hist-<n>`). Agents drive
 * the same panes through `browser_select_playlist` with these ids. The prefix
 * is deliberately NOT `usb-`, so a track-id check can never match a pane id.
 */
import type { BrowserRow } from '$lib/components/rb/browser/pane-contract.svelte';
import type { PlaylistNode } from './library-types';

// ------------------------------------------------------------- wire types

export interface UsbTrackWire {
	id: string;
	pdb_id: number;
	title: string;
	artist: string | null;
	album: string | null;
	genre: string | null;
	key: string | null;
	bpm: number | null;
	duration_s: number | null;
	rating: number;
	/** rekordbox's DJ play count, as the export stores it. */
	play_count: number;
	file_path: string;
	/** GET /audio would have served this track when the export was read.
	 * False is a file missing from a mounted stick, not a pulled stick. */
	file_present: boolean;
	has_analysis: boolean;
	has_artwork: boolean;
	date_added: string | null;
}

export interface UsbPlaylistWire {
	id: string;
	pdb_id: number;
	name: string;
	parent_id: string | null;
	is_folder: boolean;
	sort_order: number;
	track_ids: string[];
}

export interface UsbHistoryWire {
	id: string;
	name: string;
	track_ids: string[];
}

export interface UsbLibraryCounts {
	tracks: number;
	playlists: number;
	playlist_entries: number;
	history_playlists: number;
}

export interface UsbLibraryWire {
	volume_id: string;
	volume_uuid: string;
	name: string;
	mount_path: string;
	tracks: UsbTrackWire[];
	playlists: UsbPlaylistWire[];
	history: UsbHistoryWire[];
	counts: UsbLibraryCounts;
	read_ms: number;
}

/** Typed failure for everything stick-side, keyed like the backend's
 * `detail.code` so a caller branches on the code, never on the wording. */
export class UsbLibraryError extends Error {
	constructor(
		public code: string,
		message: string
	) {
		super(`${code}: ${message}`);
		this.name = 'UsbLibraryError';
	}
}

// --------------------------------------------------------------- pane ids

export const USB_PANE_PREFIX = 'usbpl:';
export const USB_ALL_TRACKS_NODE = 'all';
export const USB_HISTORY_NODE = 'history';

export interface UsbPaneRef {
	volumeUuid: string;
	nodeKey: string;
}

export function usbPaneId(volumeUuid: string, nodeKey: string): string {
	return `${USB_PANE_PREFIX}${volumeUuid}:${nodeKey}`;
}

/** The stick and node a pane id names, or null when it is not a stick pane. */
export function parseUsbPaneId(paneId: string): UsbPaneRef | null {
	if (!paneId.startsWith(USB_PANE_PREFIX)) return null;
	const rest = paneId.slice(USB_PANE_PREFIX.length);
	const colon = rest.indexOf(':');
	if (colon <= 0 || colon === rest.length - 1) return null;
	return { volumeUuid: rest.slice(0, colon), nodeKey: rest.slice(colon + 1) };
}

// ------------------------------------------------------------------- rows

/** Removed-stick rows and rows whose file is missing from a mounted stick
 * both gray out and refuse loads (TrackTable's `broken`). A pulled stick
 * outranks the per-file flag: its rows wait for the volume to come back. */
export function usbAvailability(
	stickPresent: boolean,
	filePresent: boolean
): Pick<BrowserRow, 'file_exists' | 'file_availability'> {
	if (!stickPresent) return { file_exists: false, file_availability: 'awaiting_volume' };
	else if (!filePresent) return { file_exists: false, file_availability: 'absent' };
	return { file_exists: true, file_availability: 'present' };
}

/** One stick track as a browser row. `order` is the 1-based position in the
 * node being shown, so the table's unsorted order is the stick's own order. */
export function rowFromUsbTrack(
	track: UsbTrackWire,
	volume: { volume_uuid: string; present: boolean },
	order: number
): BrowserRow {
	const idPrefix = `usb-${volume.volume_uuid}-`;
	if (!track.id.startsWith(idPrefix)) {
		throw new UsbLibraryError(
			'USB_TRACK_ID_INVALID',
			`track ${track.id} does not belong to stick ${volume.volume_uuid}`
		);
	}
	return {
		stable_id: track.id,
		item_id: null,
		order,
		title: track.title,
		artist: track.artist,
		key: track.key,
		bpm: track.bpm,
		rating: track.rating,
		energy: null,
		energy_source: null,
		energy_reason: 'stick track: energy is not read from a rekordbox export',
		etag: '',
		comments: null,
		duration_ms: track.duration_s === null ? null : Math.round(track.duration_s * 1000),
		genre: track.genre,
		genre_reason: track.genre === null ? 'no genre in the stick export' : null,
		...usbAvailability(volume.present, track.file_present),
		quality: null,
		play_count: track.play_count,
		is_streaming: false,
		is_remote: false,
		has_remote_copy: false,
		cloud_transfer: null,
		spotify_pending: false,
		strip: null,
		vocals: { status: 'not_analyzed' },
		stems: { status: 'none' },
		has_rb_mapping: false,
		artwork_available: track.has_artwork,
		artwork_status: track.has_artwork ? 'ok' : 'no_image_path',
		rb_meta: null,
		revealed: false,
		match_context: null,
		lyrics: null,
		is_remix: null,
		is_radio_edit: null
	};
}

/** The same rows for a stick that was unplugged (USBPLAY-09): they gray in
 * place, so the pane keeps its rows, order and selection. One way only: a
 * stick that comes back is re-read, because a row cannot say which of its
 * files were missing before it was pulled. */
export function withUsbStickRemoved(rows: readonly BrowserRow[]): BrowserRow[] {
	const availability = usbAvailability(false, false);
	return rows.map((row) => ({ ...row, ...availability }));
}

function _trackIdsForNode(library: UsbLibraryWire, nodeKey: string): readonly string[] {
	if (nodeKey === USB_ALL_TRACKS_NODE) return library.tracks.map((t) => t.id);
	const list =
		library.playlists.find((p) => p.id === nodeKey) ??
		library.history.find((h) => h.id === nodeKey);
	if (list === undefined) {
		throw new UsbLibraryError('USB_NODE_NOT_FOUND', `no playlist ${nodeKey} on ${library.name}`);
	}
	if ('is_folder' in list && list.is_folder) {
		throw new UsbLibraryError('USB_NODE_NOT_FOUND', `${nodeKey} is a folder, not a playlist`);
	}
	return list.track_ids;
}

/** Rows for one node of the stick tree, in rekordbox entry order. */
export function usbRowsForNode(
	library: UsbLibraryWire,
	nodeKey: string,
	present: boolean
): BrowserRow[] {
	const byId = new Map(library.tracks.map((t) => [t.id, t]));
	const volume = { volume_uuid: library.volume_uuid, present };
	return _trackIdsForNode(library, nodeKey).map((id, i) => {
		const track = byId.get(id);
		if (track === undefined) {
			throw new UsbLibraryError(
				'USB_TRACK_NOT_FOUND',
				`${nodeKey} lists ${id}, which is not in the stick's track list`
			);
		}
		return rowFromUsbTrack(track, volume, i + 1);
	});
}

/** Pane tab title: the stick's name, then the node, so two sticks never
 * read alike. */
export function usbNodeTitle(library: UsbLibraryWire, nodeKey: string): string {
	const list =
		nodeKey === USB_ALL_TRACKS_NODE
			? { name: 'All tracks' }
			: (library.playlists.find((p) => p.id === nodeKey) ??
				library.history.find((h) => h.id === nodeKey));
	if (list === undefined) {
		throw new UsbLibraryError('USB_NODE_NOT_FOUND', `no playlist ${nodeKey} on ${library.name}`);
	}
	return `${library.name.trim()} / ${list.name}`;
}

// ------------------------------------------------------------------- tree

function _leaf(volumeUuid: string, nodeKey: string, name: string, count: number): PlaylistNode {
	return {
		playlist_id: usbPaneId(volumeUuid, nodeKey),
		name,
		track_count: count,
		broken_count: 0,
		kind: 'usb',
		children: []
	};
}

function _folder(volumeUuid: string, nodeKey: string, name: string, children: PlaylistNode[]): PlaylistNode {
	return {
		playlist_id: usbPaneId(volumeUuid, nodeKey),
		name,
		track_count: children.length,
		broken_count: 0,
		kind: 'folder',
		children
	};
}

/** A stick's tree, plus the playlists it had to re-root. */
export interface UsbTree {
	nodes: PlaylistNode[];
	/** Playlists the export files under a missing parent, under a list rather
	 * than a folder, or inside a parent cycle. They are shown at the root. */
	stranded: string[];
}

function _bySortOrder(a: UsbPlaylistWire, b: UsbPlaylistWire): number {
	return a.sort_order - b.sort_order || a.pdb_id - b.pdb_id;
}

/**
 * The stick's tree: All tracks, then its playlists and folders in rekordbox
 * order (`parent_id` + `sort_order`, ties broken by pdb id so the order is
 * deterministic), then a History folder when the stick has history lists.
 *
 * Total over a parsed library: a playlist whose parent is missing or is not a
 * folder, or one caught in a parent cycle, is shown at the root after the
 * rooted playlists (the backend's flat order puts them last too) and named in
 * `stranded`. One odd row in a guest's export must never hide the rest of the
 * stick, and every playlist appears exactly once.
 */
export function buildUsbTree(library: UsbLibraryWire): UsbTree {
	const uuid = library.volume_uuid;
	const byId = new Map(library.playlists.map((p) => [p.id, p]));
	const childrenOf = new Map<string | null, UsbPlaylistWire[]>();
	for (const pl of library.playlists) {
		const siblings = childrenOf.get(pl.parent_id) ?? [];
		siblings.push(pl);
		childrenOf.set(pl.parent_id, siblings);
	}
	const placed = new Set<string>();
	const stranded: string[] = [];
	/** Nodes for `lists` in rekordbox order, skipping any already shown; a
	 * `strandedPass` records each list it places as re-rooted. */
	const unplacedNodes = (lists: readonly UsbPlaylistWire[], strandedPass = false): PlaylistNode[] =>
		lists
			.slice()
			.sort(_bySortOrder)
			.flatMap((pl) => {
				if (placed.has(pl.id)) return [];
				if (strandedPass) stranded.push(pl.id);
				return [node(pl)];
			});
	const node = (pl: UsbPlaylistWire): PlaylistNode => {
		placed.add(pl.id);
		return pl.is_folder
			? _folder(uuid, pl.id, pl.name, unplacedNodes(childrenOf.get(pl.id) ?? []))
			: _leaf(uuid, pl.id, pl.name, pl.track_ids.length);
	};
	const rooted = unplacedNodes(childrenOf.get(null) ?? []);
	const notUnderAFolder = (pl: UsbPlaylistWire): boolean =>
		pl.parent_id !== null && byId.get(pl.parent_id)?.is_folder !== true;
	// Re-root the rows whose parent is missing or a list first, so a stranded
	// folder keeps its own children; whatever is still unplaced sits in a cycle.
	const reRooted = unplacedNodes(
		library.playlists.filter((pl) => !placed.has(pl.id) && notUnderAFolder(pl)),
		true
	);
	const inCycles = unplacedNodes(
		library.playlists.filter((pl) => !placed.has(pl.id)),
		true
	);
	const nodes = [
		_leaf(uuid, USB_ALL_TRACKS_NODE, 'All tracks', library.tracks.length),
		...rooted,
		...reRooted,
		...inCycles
	];
	if (library.history.length > 0) {
		nodes.push(
			_folder(
				uuid,
				USB_HISTORY_NODE,
				'History',
				library.history.map((h) => _leaf(uuid, h.id, h.name, h.track_ids.length))
			)
		);
	}
	return { nodes, stranded };
}

// ------------------------------------------------------------ validation

function _fail(what: string): never {
	throw new UsbLibraryError('USB_LIBRARY_MALFORMED', `stick library payload: ${what}`);
}

function _str(o: Record<string, unknown>, key: string, where: string): string {
	const v = o[key];
	if (typeof v !== 'string') _fail(`${where}.${key} is not a string`);
	return v;
}

function _strOrNull(o: Record<string, unknown>, key: string, where: string): string | null {
	const v = o[key];
	if (v !== null && typeof v !== 'string') _fail(`${where}.${key} is not a string or null`);
	return v;
}

function _num(o: Record<string, unknown>, key: string, where: string): number {
	const v = o[key];
	if (typeof v !== 'number' || !Number.isFinite(v)) _fail(`${where}.${key} is not a number`);
	return v;
}

function _numOrNull(o: Record<string, unknown>, key: string, where: string): number | null {
	const v = o[key];
	if (v === null) return null;
	if (typeof v !== 'number' || !Number.isFinite(v)) _fail(`${where}.${key} is not a number or null`);
	return v;
}

function _bool(o: Record<string, unknown>, key: string, where: string): boolean {
	const v = o[key];
	if (typeof v !== 'boolean') _fail(`${where}.${key} is not a boolean`);
	return v;
}

function _obj(v: unknown, where: string): Record<string, unknown> {
	if (typeof v !== 'object' || v === null || Array.isArray(v)) _fail(`${where} is not an object`);
	return v as Record<string, unknown>;
}

function _arr(o: Record<string, unknown>, key: string, where: string): unknown[] {
	const v = o[key];
	if (!Array.isArray(v)) _fail(`${where}.${key} is not an array`);
	return v;
}

function _ids(o: Record<string, unknown>, where: string): string[] {
	return _arr(o, 'track_ids', where).map((id, i) => {
		if (typeof id !== 'string') _fail(`${where}.track_ids[${i}] is not a string`);
		return id;
	});
}

function _track(raw: unknown, i: number): UsbTrackWire {
	const where = `tracks[${i}]`;
	const o = _obj(raw, where);
	const rating = _num(o, 'rating', where);
	if (!Number.isInteger(rating) || rating < 0 || rating > 5) _fail(`${where}.rating ${rating} is not 0-5`);
	const playCount = _num(o, 'play_count', where);
	if (!Number.isInteger(playCount) || playCount < 0) _fail(`${where}.play_count ${playCount} is not a count`);
	return {
		id: _str(o, 'id', where),
		pdb_id: _num(o, 'pdb_id', where),
		title: _str(o, 'title', where),
		artist: _strOrNull(o, 'artist', where),
		album: _strOrNull(o, 'album', where),
		genre: _strOrNull(o, 'genre', where),
		key: _strOrNull(o, 'key', where),
		bpm: _numOrNull(o, 'bpm', where),
		duration_s: _numOrNull(o, 'duration_s', where),
		rating,
		play_count: playCount,
		file_path: _str(o, 'file_path', where),
		file_present: _bool(o, 'file_present', where),
		has_analysis: _bool(o, 'has_analysis', where),
		has_artwork: _bool(o, 'has_artwork', where),
		date_added: _strOrNull(o, 'date_added', where)
	};
}

function _playlist(raw: unknown, i: number): UsbPlaylistWire {
	const where = `playlists[${i}]`;
	const o = _obj(raw, where);
	return {
		id: _str(o, 'id', where),
		pdb_id: _num(o, 'pdb_id', where),
		name: _str(o, 'name', where),
		parent_id: _strOrNull(o, 'parent_id', where),
		is_folder: _bool(o, 'is_folder', where),
		sort_order: _num(o, 'sort_order', where),
		track_ids: _ids(o, where)
	};
}

function _history(raw: unknown, i: number): UsbHistoryWire {
	const where = `history[${i}]`;
	const o = _obj(raw, where);
	return { id: _str(o, 'id', where), name: _str(o, 'name', where), track_ids: _ids(o, where) };
}

/** Ids key rows, panes and the tree's keyed blocks: a repeat would show one
 * list twice or shadow All tracks / History, so it is a malformed payload. */
function _requireUnique(ids: readonly string[], what: string): void {
	const seen = new Set<string>();
	for (const id of ids) {
		if (seen.has(id)) _fail(`duplicate ${what} id ${id}`);
		seen.add(id);
	}
}

/** Validate the library payload against the route contract, field by field. */
export function parseUsbLibraryWire(raw: unknown): UsbLibraryWire {
	const o = _obj(raw, 'library');
	const counts = _obj(o['counts'], 'counts');
	const tracks = _arr(o, 'tracks', 'library').map(_track);
	const playlists = _arr(o, 'playlists', 'library').map(_playlist);
	const history = _arr(o, 'history', 'library').map(_history);
	_requireUnique(
		tracks.map((t) => t.id),
		'track'
	);
	_requireUnique(
		[
			USB_ALL_TRACKS_NODE,
			USB_HISTORY_NODE,
			...playlists.map((p) => p.id),
			...history.map((h) => h.id)
		],
		'list'
	);
	return {
		volume_id: _str(o, 'volume_id', 'library'),
		volume_uuid: _str(o, 'volume_uuid', 'library'),
		name: _str(o, 'name', 'library'),
		mount_path: _str(o, 'mount_path', 'library'),
		tracks,
		playlists,
		history,
		counts: {
			tracks: _num(counts, 'tracks', 'counts'),
			playlists: _num(counts, 'playlists', 'counts'),
			playlist_entries: _num(counts, 'playlist_entries', 'counts'),
			history_playlists: _num(counts, 'history_playlists', 'counts')
		},
		read_ms: _num(o, 'read_ms', 'library')
	};
}
