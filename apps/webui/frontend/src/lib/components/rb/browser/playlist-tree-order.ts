/**
 * Playlist tree order (LIBTREE-01..02, ADR-NEW-playlist-tree-order-overlay).
 *
 * The tree sorts on each playlist's `seq`: the user's own order when one is
 * stored (PUT /api/v1/playlist-tree/order), else the rekordbox Seq walk, and
 * playlists with neither sink below, name-sorted. This module is the pure
 * half: the one sort rule, a drag move, and Sort A-Z, each over the WHOLE
 * playlist list (never the tree's filtered view, so a playlist hidden by the
 * Broken filter keeps its slot instead of being pushed to the bottom).
 *
 * Pure: no Svelte, no fetch, so node tests can load it.
 */

import { isInboxPlaylistId } from './inbox-id';

export interface OrderablePlaylist {
	playlist_id: string;
	name: string;
	seq?: number | null;
}

export type DropPlace = 'before' | 'after';

/** The tree's sort rule: seq ascending, then playlists with no seq by name. */
export function compareTreeOrder(a: OrderablePlaylist, b: OrderablePlaylist): number {
	if (isInboxPlaylistId(a.playlist_id) !== isInboxPlaylistId(b.playlist_id)) {
		return isInboxPlaylistId(a.playlist_id) ? -1 : 1;
	}
	// `seq` is optional as well as nullable in PlaylistSummary, so an absent
	// key means the same "no order known" as a null one.
	const aSeq = a.seq ?? null;
	const bSeq = b.seq ?? null;
	if (aSeq !== null && bSeq !== null) return aSeq - bSeq;
	if (aSeq !== null) return -1;
	if (bSeq !== null) return 1;
	return a.name.localeCompare(b.name);
}

/** Every playlist id, top to bottom, as the tree shows them. */
export function treeOrderIds(playlists: readonly OrderablePlaylist[]): string[] {
	return playlists
		.slice()
		.sort(compareTreeOrder)
		.map((p) => p.playlist_id);
}

/**
 * `order` with `moved` taken out and put back just before or after `target`.
 * Returns null when the drop changes nothing (onto itself, an unknown id, or
 * the slot it already holds), so the caller writes nothing.
 */
export function movePlaylistInOrder(
	order: readonly string[],
	moved: string,
	target: string,
	place: DropPlace
): string[] | null {
	if (moved === target || !order.includes(moved) || !order.includes(target)) return null;
	const rest = order.filter((id) => id !== moved);
	const at = rest.indexOf(target) + (place === 'after' ? 1 : 0);
	const next = [...rest.slice(0, at), moved, ...rest.slice(at)];
	return next.every((id, i) => id === order[i]) ? null : next;
}

/** Every playlist id sorted A-Z by name (case-insensitive, numbers in order). */
export function nameSortedIds(playlists: readonly OrderablePlaylist[]): string[] {
	return playlists
		.slice()
		.sort((a, b) => {
			if (isInboxPlaylistId(a.playlist_id) !== isInboxPlaylistId(b.playlist_id)) {
				return isInboxPlaylistId(a.playlist_id) ? -1 : 1;
			}
			return (
				a.name.localeCompare(b.name, undefined, { sensitivity: 'base', numeric: true }) ||
				a.playlist_id.localeCompare(b.playlist_id)
			);
		})
		.map((p) => p.playlist_id);
}

/** The playlists with `seq` restamped from `order`, for an optimistic repaint
 * before the server answers. Ids `order` leaves out keep their seq, shifted
 * after it, the same way the server ranks them. */
export function withTreeOrder<T extends OrderablePlaylist>(playlists: readonly T[], order: readonly string[]): T[] {
	const position = new Map(order.map((id, i) => [id, i]));
	return playlists.map((p) => {
		const at = position.get(p.playlist_id);
		if (at !== undefined) return { ...p, seq: at };
		const seq = p.seq ?? null;
		return { ...p, seq: seq === null ? null : order.length + seq };
	});
}

/** Where a drop lands on a row: the upper half is before it, the lower after. */
export function dropPlaceAt(clientY: number, rowTop: number, rowHeight: number): DropPlace {
	return clientY < rowTop + rowHeight / 2 ? 'before' : 'after';
}

/** What {@link createTreeOrderWriter} needs from the panel. */
export interface TreeOrderWriterHooks<T extends OrderablePlaylist> {
	read(): readonly T[];
	write(rows: T[]): void;
	put(order: string[]): Promise<unknown>;
	refresh(): Promise<void>;
	notify(message: string, kind: 'info' | 'error'): void;
}

/** LIBTREE-01/02: one writer for tree order changes. Repaints at once, then
 * writes. At most one PUT is in flight; a newer order waits for it and
 * replaces any order still queued, so the newest arrangement is always the
 * last one stored. When the newest write fails, the rows from before the
 * first unsaved change are put back without needing the server, and only
 * then is a refresh tried.
 *
 * `busy()` is true while an order is queued or its PUT is in flight. Codex P2
 * on #6186 (r4265141178): the first PUT of a burst publishes a playlists
 * event whose GET can finish before the queued PUT is stored, so a read that
 * lands while busy() would paint the older order over the newer optimistic
 * one.
 *
 * `overlay(rows)` is what every playlist reader applies (Codex P2 on #6186,
 * r4266059809): while busy it restamps the read rows with the newest
 * optimistic order, so a create, rename or delete the read carries still
 * shows, but the order on screen stays the one being written. When idle it
 * returns the rows untouched. The writer's own refresh, run once idle,
 * repaints from what the server finally holds. */
export interface TreeOrderWriter {
	(order: string[], done?: string): Promise<void>;
	busy(): boolean;
	overlay<R extends OrderablePlaylist>(rows: R[]): R[];
}

/** `rows` with each seq put back from `baseline` (an id to seq map); rows the
 * baseline never saw keep their own seq. Only the ORDER is rolled back: names,
 * rows created since and rows deleted since all stay as `rows` has them
 * (Codex P2 on #6186, r4266059812). */
export function withSeqFrom<T extends OrderablePlaylist>(
	rows: readonly T[],
	baseline: ReadonlyMap<string, number | null>
): T[] {
	return rows.map((p) => (baseline.has(p.playlist_id) ? { ...p, seq: baseline.get(p.playlist_id) ?? null } : p));
}

export function createTreeOrderWriter<T extends OrderablePlaylist>(
	hooks: TreeOrderWriterHooks<T>
): TreeOrderWriter {
	let snapshot: T[] | null = null;
	let pending: { order: string[]; done: string | undefined } | null = null;
	let draining: Promise<void> | null = null;
	let putting = false;
	let latest: string[] | null = null;
	const busy = (): boolean => putting || pending !== null;

	async function drain(): Promise<void> {
		while (pending !== null) {
			const job = pending;
			pending = null;
			let failure: unknown = null;
			putting = true;
			try {
				await hooks.put(job.order);
			} catch (exc) {
				failure = exc;
			} finally {
				putting = false;
			}
			// Codex P2 on #6186: a stored order is the new rollback baseline, so a
			// later failed write in the same burst restores what the server holds,
			// not the rows from before the whole burst.
			if (failure === null && snapshot !== null) snapshot = withTreeOrder(snapshot, job.order);
			// A newer order is queued: it replaces this one in full, so this
			// write's outcome no longer decides what the tree shows.
			if (pending !== null) continue;
			const restore = snapshot;
			snapshot = null;
			latest = null;
			if (failure === null) {
				if (job.done !== undefined) hooks.notify(job.done, 'info');
			} else {
				if (restore !== null) {
					const baseline = new Map(restore.map((p) => [p.playlist_id, p.seq ?? null]));
					hooks.write(withSeqFrom(hooks.read(), baseline));
				}
				hooks.notify(String(failure instanceof Error ? failure.message : failure), 'error');
			}
			try {
				await hooks.refresh();
			} catch {
				// The rows already show the stored (or restored) order.
			}
		}
		// Cleared in the same tick the loop sees no pending order, so a drag
		// arriving later always starts a fresh drain instead of being stranded.
		draining = null;
	}

	const write = (order: string[], done?: string): Promise<void> => {
		if (snapshot === null) snapshot = hooks.read().slice();
		hooks.write(withTreeOrder(hooks.read(), order));
		pending = { order, done };
		latest = order;
		if (draining === null) draining = drain();
		return draining;
	};
	return Object.assign(write, {
		busy,
		overlay: <R extends OrderablePlaylist>(rows: R[]): R[] =>
			busy() && latest !== null ? withTreeOrder(rows, latest) : rows
	});
}

/**
 * Codex P2 (BLOCKING) on #6186: every stored order publishes a playlists
 * event, and each event starts a playlist re-read; creates and the blank
 * sweep re-read too. Reads can finish out of order, so an older GET would
 * repaint an older order over a newer one. Every read shares one sequence:
 * only the most recently started read applies its rows (each caller passes
 * its own apply), and an older read that finishes later is dropped.
 * Resolves true when this read applied.
 */
export function createLatestRead<T>(
	load: () => Promise<T>
): (apply: (value: T) => void | Promise<void>) => Promise<boolean> {
	let started = 0;
	return async (apply) => {
		started += 1;
		const mine = started;
		const value = await load();
		if (mine !== started) return false;
		await apply(value);
		return true;
	};
}
