/** Track context-menu targeting and first-action runners (issue #2286). */

import { ConflictError, createPairing, getTrack, patchTrack } from '$lib/api';
import { backfillProgress, enqueueBackfill } from '$lib/rb/api-analysis-backfill';
import {
	describeReanalyzePlan,
	formatReanalyzeEnqueueToast,
	REANALYZE_BACKEND,
	REANALYZE_LANE,
	REANALYZE_TOAST_GROUP,
	toReanalyzeToastPresentation,
	watchReanalyzeBatch
} from '$lib/rb/reanalyze-batch-feedback';
import { revealTrack } from '$lib/rb/api-track-reveal';
import { isUsbTrackId } from '$lib/rb/track-source';
import { pushToast } from '$lib/stores.svelte';
import { announcePairingsChanged } from '$lib/rb/pairings-changed';
import { normalizeRating } from '$lib/rating';
import { invalidateLibraryIndex } from '$lib/rb/library-index';
import { trackColourName } from './track-table-column-filters';
import { fetchOpenDjPlayCounts } from '$lib/plays/play-counter';
import {
	reorderItemIdsViaMove,
	weightedShuffleItemIds,
	type PlaylistShuffleMember
} from '$lib/rb/playlist-shuffle';

export const REVEAL_TRACK_TITLE = 'POST /api/v1/tracks/{stable_id}:reveal';

/**
 * The right-click menu's registry chunk failed to load (W28, CTXMENU-07).
 * Reload is the honest remedy: Chromium caches a failed module fetch for the
 * life of the page, so a second import() rejects without refetching.
 */
export const MENU_LOAD_FAILED = 'Right-click menu failed to load. Reload Open DJ to try again.';

export function reportMenuLoadFailure(cause: unknown): void {
	pushToast(
		MENU_LOAD_FAILED,
		'error',
		undefined,
		cause,
		{ surface: 'context-menu registry chunk' },
		undefined,
		undefined,
		{ headline: MENU_LOAD_FAILED, feature: 'Right-click menu' }
	);
}

export type ToastFn = (message: string, kind: 'info' | 'warn' | 'error', duration?: number) => void;

/**
 * The menu a row may show. A stick row (Play from USB) is read only and has
 * no library row behind it, so every item that calls a library-only API (add
 * to playlist, edit, relocate, reveal, copy path, re-analyze, stem / lyrics
 * jobs, remove) is dropped rather than left enabled to be refused by the
 * server. Deck loads are the stick row's real actions. An allowlist, so an
 * item added to the menu later stays off stick rows until it is proven to
 * work for them.
 */
export function menuItemsForTrack<T extends { id: string }>(
	stableId: string,
	items: readonly T[]
): T[] {
	return isUsbTrackId(stableId) ? items.filter((item) => item.id.startsWith('load-')) : [...items];
}

export function menuTargetIds(
	row: { stable_id: string; order: number },
	selectedOrders: readonly number[],
	selectedIds: readonly string[]
): string[] {
	return selectedOrders.includes(row.order) ? [...selectedIds] : [row.stable_id];
}

/**
 * The library rows the menu's target covers, by position. Duplicate rows share
 * one stable id, so `menuTargetIds(...).length` can be smaller than the rows the
 * user selected. PAIR-10 counts rows, so the pair rule reads this instead.
 */
export function menuTargetRowCount(
	row: { order: number },
	selectedOrders: readonly number[]
): number {
	return selectedOrders.includes(row.order) ? selectedOrders.length : 1;
}

export function isStreamingUri(path: string | null | undefined): boolean {
	if (!path) return true;
	const lower = path.toLowerCase();
	return (
		lower.startsWith('tidal:') || lower.startsWith('spotify:') || lower.startsWith('soundcloud:')
	);
}

export async function runCopyPaths(
	targetIds: readonly string[],
	deps: {
		getTrack: (id: string) => Promise<{ track: { file_path?: string | null } }>;
		writeClipboard: (text: string) => Promise<void>;
		pushToast: ToastFn;
	} = {
		getTrack,
		writeClipboard: (text) => navigator.clipboard.writeText(text),
		pushToast
	}
): Promise<void> {
	const paths: string[] = [];
	let firstMissingId: string | null = null;
	for (const id of targetIds) {
		const { track } = await deps.getTrack(id);
		const filePath = track.file_path;
		if (!filePath || isStreamingUri(filePath)) {
			if (firstMissingId === null) firstMissingId = id;
			continue;
		}
		paths.push(filePath);
	}
	if (paths.length === 0) {
		deps.pushToast(`no file_path on GET /api/v1/tracks/${firstMissingId ?? targetIds[0]}`, 'error');
		return;
	}
	try {
		await deps.writeClipboard(paths.join('\n'));
	} catch (error) {
		deps.pushToast(error instanceof Error ? error.message : String(error), 'error');
		return;
	}
	deps.pushToast(`Copied ${paths.length} path${paths.length === 1 ? '' : 's'}`, 'info');
}

export async function runRevealTracks(
	targetIds: readonly string[],
	deps: {
		revealTrack: (stableId: string) => Promise<void>;
		pushToast: ToastFn;
	} = { revealTrack, pushToast }
): Promise<void> {
	let revealed = 0;
	let firstError: string | null = null;
	for (const id of targetIds) {
		try {
			await deps.revealTrack(id);
			revealed += 1;
		} catch (error) {
			if (firstError === null) {
				firstError = error instanceof Error ? error.message : String(error);
			}
		}
	}
	if (revealed > 0) {
		// Partial success names the total so the toast never reads as full success.
		const total = revealed < targetIds.length ? ` of ${targetIds.length}` : '';
		const noun = `track${(total ? targetIds.length : revealed) === 1 ? '' : 's'}`;
		deps.pushToast(`Revealed ${revealed}${total} ${noun}`, 'info');
	}
	if (firstError !== null) {
		deps.pushToast(firstError, 'error');
	}
}

function _pushReanalyzeToast(
	push: typeof pushToast,
	presentation: { message: string; kind: 'info' | 'warn' | 'error'; title?: string }
): void {
	const { headline, detail, kind } = toReanalyzeToastPresentation(presentation);
	push(presentation.message, kind, undefined, undefined, {}, REANALYZE_TOAST_GROUP, undefined, {
		headline,
		detail,
		feature: 'Analysis'
	});
}

export async function runReanalyze(
	targetIds: readonly string[],
	deps: {
		enqueueBackfill: typeof enqueueBackfill;
		backfillProgress: typeof backfillProgress;
		pushToast: typeof pushToast;
		watchBatch: typeof watchReanalyzeBatch;
	} = {
		enqueueBackfill,
		backfillProgress,
		pushToast,
		watchBatch: watchReanalyzeBatch
	}
): Promise<void> {
	const planLabel = describeReanalyzePlan(REANALYZE_LANE, REANALYZE_BACKEND);
	try {
		const result = await deps.enqueueBackfill({
			stableIds: [...targetIds],
			lane: REANALYZE_LANE,
			backend: REANALYZE_BACKEND,
			note: 'track context menu',
			drain: true
		});
		let progress = null;
		try {
			progress = await deps.backfillProgress(result.batch_id);
		} catch {
			progress = null;
		}
		_pushReanalyzeToast(deps.pushToast, formatReanalyzeEnqueueToast(result, progress, planLabel));

		if (result.admitted > 0) {
			deps.watchBatch(result.batch_id, planLabel, {
				onUpdate: (_progress, presentation) => _pushReanalyzeToast(deps.pushToast, presentation),
				onTerminal: (_progress, presentation) => _pushReanalyzeToast(deps.pushToast, presentation),
				onError: (message) =>
					deps.pushToast(message, 'error', undefined, undefined, {}, REANALYZE_TOAST_GROUP)
			});
		}
	} catch (error) {
		// #12578: same group key so a startup failure replaces any "Waiting for the queue" toast.
		deps.pushToast(
			error instanceof Error ? error.message : String(error),
			'error',
			undefined,
			error,
			{},
			REANALYZE_TOAST_GROUP
		);
	}
}

/** Issue #9451: set star rating 0-5 on one or more library rows (same PATCH path as BrowserPanel). */
export async function runSetRating(
	targetIds: readonly string[],
	rating: number,
	deps: {
		getTrack: typeof getTrack;
		patchTrack: typeof patchTrack;
		pushToast: ToastFn;
	} = { getTrack, patchTrack, pushToast }
): Promise<void> {
	let ok = 0;
	let firstError: string | null = null;
	for (const id of targetIds) {
		if (isUsbTrackId(id)) {
			firstError = firstError ?? 'stick tracks are read only: the rating was not saved';
			continue;
		}
		try {
			let etag = '';
			const first = await deps.getTrack(id);
			etag = first.etag;
			await deps.patchTrack(id, etag, { rating });
			ok += 1;
		} catch (error) {
			if (error instanceof ConflictError) {
				try {
					await deps.patchTrack(id, error.etag, { rating });
					ok += 1;
				} catch (e2) {
					if (firstError === null) firstError = e2 instanceof Error ? e2.message : String(e2);
				}
			} else if (firstError === null) {
				firstError = error instanceof Error ? error.message : String(error);
			}
		}
	}
	if (ok > 0) {
		invalidateLibraryIndex();
		deps.pushToast(
			`Rating set (${normalizeRating(rating) ?? rating}) on ${ok} track${ok === 1 ? '' : 's'}`,
			'info'
		);
	}
	if (firstError !== null) {
		deps.pushToast(firstError, 'error');
	}
}

/** Issue #9737: set (1..8) or clear (null) the rekordbox colour label on every selected row. */
export async function runSetColour(
	targetIds: readonly string[],
	colorId: number | null,
	deps: {
		getTrack: typeof getTrack;
		patchTrack: typeof patchTrack;
		pushToast: ToastFn;
	} = { getTrack, patchTrack, pushToast }
): Promise<void> {
	let ok = 0;
	let firstError: string | null = null;
	for (const id of targetIds) {
		if (isUsbTrackId(id)) {
			firstError = firstError ?? 'stick tracks are read only: the colour was not saved';
			continue;
		}
		try {
			const first = await deps.getTrack(id);
			await deps.patchTrack(id, first.etag, { color_id: colorId });
			ok += 1;
		} catch (error) {
			if (error instanceof ConflictError) {
				try {
					await deps.patchTrack(id, error.etag, { color_id: colorId });
					ok += 1;
				} catch (e2) {
					if (firstError === null) firstError = e2 instanceof Error ? e2.message : String(e2);
				}
			} else if (firstError === null) {
				firstError = error instanceof Error ? error.message : String(error);
			}
		}
	}
	if (ok > 0) {
		invalidateLibraryIndex();
		const what = colorId === null ? 'Colour cleared' : `Colour set (${trackColourName(colorId)})`;
		deps.pushToast(`${what} on ${ok} track${ok === 1 ? '' : 's'}`, 'info');
	}
	if (firstError !== null) {
		deps.pushToast(firstError, 'error');
	}
}

/** NOWP-11487: hide or show one or more library rows in tracklist exports. */
export async function runSetTracklistHidden(
	targetIds: readonly string[],
	hidden: boolean,
	deps: {
		getTrack: typeof getTrack;
		patchTrack: typeof patchTrack;
		pushToast: ToastFn;
	} = { getTrack, patchTrack, pushToast }
): Promise<void> {
	let ok = 0;
	let firstError: string | null = null;
	for (const id of targetIds) {
		if (isUsbTrackId(id)) {
			firstError = firstError ?? 'stick tracks are read only: tracklist visibility was not saved';
			continue;
		}
		try {
			const first = await deps.getTrack(id);
			await deps.patchTrack(id, first.etag, { tracklist_hidden: hidden });
			ok += 1;
		} catch (error) {
			if (error instanceof ConflictError) {
				try {
					await deps.patchTrack(id, error.etag, { tracklist_hidden: hidden });
					ok += 1;
				} catch (e2) {
					if (firstError === null) firstError = e2 instanceof Error ? e2.message : String(e2);
				}
			} else if (firstError === null) {
				firstError = error instanceof Error ? error.message : String(error);
			}
		}
	}
	if (ok > 0) {
		invalidateLibraryIndex();
		const verb = hidden ? 'Hidden in tracklists' : 'Shown in tracklists';
		deps.pushToast(`${verb} (${ok} track${ok === 1 ? '' : 's'})`, 'info');
	}
	if (firstError !== null) {
		deps.pushToast(firstError, 'error');
	}
}

/**
 * PAIR-10: "Pair these 2 tracks" makes a LOOSE pair: no snapshot, so loading
 * either track later loads it like any other. The route publishes
 * `library.changed` kind=pairings, which repaints the paired-row edges.
 */
export type PlaylistShuffleRow = {
	item_id: string;
	stable_id: string;
};

export type PlaylistShuffleDeps = {
	rows: readonly PlaylistShuffleRow[];
	playlistId: string;
	etag: string;
	/** False when read-only, sorted, searched, or truncated. */
	allowed: boolean;
	blockedReason: string | null;
	move: typeof reorderItemIdsViaMove;
	fetchCounts: typeof fetchOpenDjPlayCounts;
	seed: () => number;
	reload: () => Promise<void>;
	pushToast: ToastFn;
};

export async function runPlaylistShuffle(deps: PlaylistShuffleDeps): Promise<void> {
	if (!deps.allowed) {
		deps.pushToast(
			deps.blockedReason ?? 'Shuffle needs an editable playlist in membership order',
			'error'
		);
		return;
	}
	const members: PlaylistShuffleMember[] = [];
	for (const row of deps.rows) {
		if (row.item_id === null || row.item_id === '') {
			deps.pushToast('playlist row has no membership id - reload and try again', 'error');
			return;
		}
		members.push({ item_id: row.item_id, stable_id: row.stable_id });
	}
	if (members.length < 2) {
		deps.pushToast('Playlist needs at least 2 tracks to shuffle', 'info');
		return;
	}
	let counts: Map<string, number>;
	try {
		counts = await deps.fetchCounts(members.map((m) => m.stable_id));
	} catch (error) {
		deps.pushToast(`play counts unavailable: ${String(error)}`, 'error');
		return;
	}
	const current = members.map((m) => m.item_id);
	const target = weightedShuffleItemIds(members, (sid) => counts.get(sid) ?? 0, deps.seed() >>> 0);
	try {
		await deps.move(deps.playlistId, deps.etag, current, target);
		await deps.reload();
		invalidateLibraryIndex();
		deps.pushToast(`Shuffled ${members.length} tracks`, 'info');
	} catch (error) {
		deps.pushToast(`shuffle failed: ${String(error)}`, 'error');
	}
}

export async function runPairTracks(targetIds: readonly string[]): Promise<void> {
	if (targetIds.length !== 2 || targetIds[0] === targetIds[1]) {
		pushToast('Pairing needs exactly 2 different tracks', 'error');
		return;
	}
	const [from_stable_id, to_stable_id] = targetIds as [string, string];
	try {
		await createPairing({
			from_stable_id,
			to_stable_id,
			direction: '<->',
			source: 'manual',
			notes: null,
			snapshot: null
		});
	} catch (error) {
		pushToast(error instanceof Error ? error.message : String(error), 'error');
		return;
	}
	announcePairingsChanged();
	pushToast('Paired 2 tracks (loose: no alignment)', 'info');
}
