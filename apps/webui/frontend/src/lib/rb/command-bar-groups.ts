/**
 * CMDK-04: the v2 Cmd K bar's search, as pure functions over rows already in
 * memory.
 *
 * Every search covers the open playlist AND the whole collection in one pass
 * over rows the browser already holds (the pane's rows and the LIBM-171
 * library index), so both groups arrive together and nothing streams. Playlist
 * hits list first; the collection group never repeats a track the playlist
 * group already shows.
 *
 * A row matches when every whitespace-separated token appears in one of its
 * searchable fields, in any order. The haystack for a row is built once and
 * cached on the row object, so a keystroke only scans strings (measured in the
 * design round: about 1.5 ms over 11,165 rows in Node).
 *
 * Runeless and import-free so the unit suite loads it directly.
 */

/** The fields the bar searches and shows. Index rows and pane rows both fit. */
export interface SearchableRow {
	stable_id: string;
	title: string | null;
	artist: string | null;
	album?: string | null;
	genre?: string | null;
	key: string | null;
	bpm: number | null;
	comments?: string | null;
	notes?: string | null;
}

/** Fields a match can come from that the row's title line does not show. */
export type ViaField = 'album' | 'genre' | 'comments' | 'notes';

const VIA_FIELDS: readonly ViaField[] = ['album', 'genre', 'comments', 'notes'];

/** Field separator: a token can never match across two fields. */
const SEP = '\u0001';

const haystackCache = new WeakMap<object, string>();

function fmtBpm(bpm: number | null): string {
	return bpm === null || !Number.isFinite(bpm) ? '' : bpm.toFixed(1);
}

/** Lower-cased searchable text for one row, cached per row object. */
export function cmdkHaystack(row: SearchableRow): string {
	const hit = haystackCache.get(row);
	if (hit !== undefined) return hit;
	const parts = [
		row.title,
		row.artist,
		row.album,
		row.genre,
		row.key,
		fmtBpm(row.bpm),
		row.comments,
		row.notes
	];
	const text = parts
		.filter((p): p is string => typeof p === 'string' && p !== '')
		.join(SEP)
		.toLowerCase();
	haystackCache.set(row, text);
	return text;
}

/** Whether a row's haystack is already built (so a search will not build it). */
export function haystackCached(row: SearchableRow): boolean {
	return haystackCache.has(row);
}

/**
 * Build the cached haystacks of rows[start..] until `keepGoing()` says stop,
 * checked every 256 rows. Returns the next unbuilt index (rows.length when
 * done). The bar calls it in short idle slices as it opens, so the cold build
 * (70 ms at 50,000 rows) runs off the keystroke (spec AC-P2). The bar holds
 * a typed search until the last slice lands, so no keystroke builds the rest.
 */
export function warmHaystacks(rows: readonly SearchableRow[], start: number, keepGoing: () => boolean): number {
	let i = start;
	while (i < rows.length) {
		cmdkHaystack(rows[i]);
		i += 1;
		if ((i & 255) === 0 && !keepGoing()) break;
	}
	return i;
}

/** The query's tokens: lower-cased, whitespace-split, empty ones dropped. */
export function queryTokens(query: string): string[] {
	return query.toLowerCase().split(/\s+/).filter((t) => t !== '');
}

/** Every token appears somewhere in the row's haystack. No tokens matches without building one. */
export function rowMatches(row: SearchableRow, tokens: readonly string[]): boolean {
	if (tokens.length === 0) return true;
	const hay = cmdkHaystack(row);
	for (const t of tokens) if (!hay.includes(t)) return false;
	return true;
}

/**
 * Which hidden fields explain the match: one entry per field needed to cover
 * a token the row's visible text (title, artist, key, BPM) does not show, in
 * VIA_FIELDS order, each field at most once. Empty when the visible text
 * already explains every token. Drives the small "album: ..." chips, so a hit
 * on a field the user cannot see never looks like a wrong result.
 */
export function matchedVia(
	row: SearchableRow,
	tokens: readonly string[]
): { field: ViaField; value: string }[] {
	const shown = [row.title, row.artist, row.key, fmtBpm(row.bpm)]
		.filter((p): p is string => typeof p === 'string' && p !== '')
		.join(SEP)
		.toLowerCase();
	const needed = new Set<ViaField>();
	for (const token of tokens) {
		if (shown.includes(token)) continue;
		for (const field of VIA_FIELDS) {
			const value = row[field];
			if (typeof value === 'string' && value.toLowerCase().includes(token)) {
				needed.add(field);
				break;
			}
		}
	}
	const out: { field: ViaField; value: string }[] = [];
	for (const field of VIA_FIELDS) {
		const value = row[field];
		if (needed.has(field) && typeof value === 'string') out.push({ field, value });
	}
	return out;
}

export interface CmdkGroup<Row> {
	/** Rows to draw, at most `limit`. */
	rows: Row[];
	/** Every row that matched and passed the filters, drawn or not. */
	total: number;
}

export interface CmdkGroups<P, C> {
	playlist: CmdkGroup<P>;
	collection: CmdkGroup<C>;
	/** Rows that matched the text but a filter hid, across both groups. */
	hiddenByFilters: number;
}

/**
 * Search the open playlist and the collection at once.
 *
 * `keep` is the active filter set (the library's own toggles); a row it
 * rejects is counted in `hiddenByFilters` rather than vanishing silently.
 * Pass one predicate per group when they differ: the open pane's own
 * exemptions (Missing Tracks keeps its broken rows) cover only its rows, never
 * the collection. The collection group skips every stable_id the playlist
 * group shows, so a track shows once, in the playlist group. A playlist row
 * its filter hid does not hide the collection's row for the same track: the
 * pane can lag the index after a library change (say a file that came back),
 * and the collection row is then the usable one. A track is counted hidden at
 * most once, and not at all when either group shows it.
 */
export function searchGroups<P extends SearchableRow, C extends SearchableRow>(
	playlistRows: readonly P[],
	collectionRows: readonly C[],
	query: string,
	keep: ((row: P | C) => boolean) | { playlist: (row: P) => boolean; collection: (row: C) => boolean },
	limit: number
): CmdkGroups<P, C> {
	if (!(limit > 0)) throw new Error(`searchGroups: limit must be positive, got ${limit}`);
	const keepPlaylist = typeof keep === 'function' ? keep : keep.playlist;
	const keepCollection = typeof keep === 'function' ? keep : keep.collection;
	const tokens = queryTokens(query);
	const shown = new Set<string>();
	const hiddenInPlaylist = new Set<string>();
	let hidden = 0;
	const playlist: CmdkGroup<P> = { rows: [], total: 0 };
	for (const row of playlistRows) {
		if (!rowMatches(row, tokens)) continue;
		if (!keepPlaylist(row)) {
			hidden += 1;
			hiddenInPlaylist.add(row.stable_id);
			continue;
		}
		shown.add(row.stable_id);
		playlist.total += 1;
		if (playlist.rows.length < limit) playlist.rows.push(row);
	}
	const collection: CmdkGroup<C> = { rows: [], total: 0 };
	// Nothing typed: the bar lists the open playlist as it stands, and the
	// whole collection is not a useful list to scroll.
	if (tokens.length === 0) return { playlist, collection, hiddenByFilters: hidden };
	for (const row of collectionRows) {
		if (shown.has(row.stable_id) || !rowMatches(row, tokens)) continue;
		const hiddenAbove = hiddenInPlaylist.has(row.stable_id);
		if (!keepCollection(row)) {
			if (!hiddenAbove) hidden += 1;
			continue;
		}
		if (hiddenAbove) hidden -= 1;
		collection.total += 1;
		if (collection.rows.length < limit) collection.rows.push(row);
	}
	return { playlist, collection, hiddenByFilters: hidden };
}

/** A library playlist the bar can open. */
interface SearchableList {
	playlist_id: string;
	name: string;
}

/**
 * CMDK-6848: library playlists whose name holds every token, in the order
 * given (the tree's). Nothing typed matches none, so the bar still opens on
 * the open playlist's tracks.
 */
export function matchLists<L extends SearchableList>(
	lists: readonly L[],
	query: string,
	limit: number
): CmdkGroup<L> {
	if (!(limit > 0)) throw new Error(`matchLists: limit must be positive, got ${limit}`);
	const tokens = queryTokens(query);
	const group: CmdkGroup<L> = { rows: [], total: 0 };
	if (tokens.length === 0) return group;
	for (const list of lists) {
		const name = list.name.toLowerCase();
		if (!tokens.every((t) => name.includes(t))) continue;
		group.total += 1;
		if (group.rows.length < limit) group.rows.push(list);
	}
	return group;
}

/**
 * First index of each layout group in the drawn / flat order.
 * When listCount > 0, library playlist rows sit after the playlist
 * group's tracks, so every later group starts listCount later.
 * With no playlist group, starts are the prefix sums of entries
 * (lists are not drawn then).
 */
export function cmdkGroupStarts(
	groups: readonly { id: string; entries: { readonly length: number } }[],
	listCount: number
): number[] {
	const starts: number[] = [];
	let at = 0;
	for (const group of groups) {
		starts.push(at);
		at += group.entries.length;
		if (listCount > 0 && group.id === 'playlist') at += listCount;
	}
	return starts;
}

/** Concatenated-entries index where library playlist rows splice in. */
export function cmdkListSpliceAt(
	groups: readonly { id: string; entries: { readonly length: number } }[]
): number {
	let at = 0;
	for (const group of groups) {
		if (group.id === 'playlist') return at + group.entries.length;
		at += group.entries.length;
	}
	return 0;
}

/**
 * Keys for a keyed each over drawn rows. A playlist may hold one track more
 * than once (LIBM-03), so stable_id alone repeats and Svelte refuses duplicate
 * keys. A row's membership item_id wins when it has one; otherwise stable_id
 * plus how many times that id has come before, so the same rows give the same
 * keys on every render.
 */
export function rowKeys(rows: readonly (SearchableRow & { item_id?: string | null })[]): string[] {
	const seen = new Map<string, number>();
	return rows.map((row) => {
		if (typeof row.item_id === 'string' && row.item_id !== '') return `item:${row.item_id}`;
		const n = seen.get(row.stable_id) ?? 0;
		seen.set(row.stable_id, n + 1);
		return `sid:${row.stable_id}:${n}`;
	});
}

/**
 * Comments and notes always come from the index row when there is one: an All
 * Tracks pane row carries the track's notes in its `comments` slot
 * (rowFromListWire), so trusting the pane would search notes as comments and
 * miss the real comment.
 */
const INDEX_WINS: ReadonlySet<ViaField> = new Set<ViaField>(['comments', 'notes']);

/**
 * Pane rows (BrowserRow) do not carry every searched field: album and notes
 * come only from the library index. Fill the hidden search fields a pane row
 * lacks from the index row with the same stable_id, so a playlist track that
 * matches only by album lists under the open playlist rather than falling
 * through to the collection group. For album and genre a value the pane row
 * already has wins; comments and notes are taken from the index row
 * (INDEX_WINS). A row with nothing to change is returned as is (same object).
 */
export function fillSearchFields<P extends SearchableRow>(
	paneRows: readonly P[],
	indexRows: readonly SearchableRow[]
): P[] {
	if (indexRows.length === 0) return [...paneRows];
	const byId = new Map<string, SearchableRow>();
	for (const row of indexRows) byId.set(row.stable_id, row);
	return paneRows.map((row) => {
		const held = byId.get(row.stable_id);
		if (held === undefined) return row;
		let filled: P | null = null;
		for (const field of VIA_FIELDS) {
			if (INDEX_WINS.has(field)) {
				if ((row[field] ?? null) === (held[field] ?? null)) continue;
			} else if (row[field] != null || held[field] == null) continue;
			filled ??= { ...row };
			filled[field] = held[field] ?? null;
		}
		return filled ?? row;
	});
}

/** Split text into runs that do and do not match a token, for underlining hits. */
export function highlightParts(text: string, tokens: readonly string[]): { text: string; hit: boolean }[] {
	const lower = text.toLowerCase();
	const marks = new Array<boolean>(text.length).fill(false);
	for (const t of tokens) {
		if (t === '') continue;
		for (let at = lower.indexOf(t); at >= 0; at = lower.indexOf(t, at + 1)) {
			for (let i = at; i < at + t.length; i += 1) marks[i] = true;
		}
	}
	const out: { text: string; hit: boolean }[] = [];
	for (let i = 0; i < text.length; i += 1) {
		const last = out[out.length - 1];
		if (last !== undefined && last.hit === marks[i]) last.text += text[i];
		else out.push({ text: text[i], hit: marks[i] });
	}
	return out;
}
