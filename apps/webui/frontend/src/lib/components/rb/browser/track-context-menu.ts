/** Track context-menu targeting and first-action runners (issue #2286). */

import { getTrack } from '$lib/api';
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

export const REVEAL_TRACK_TITLE = 'POST /api/v1/tracks/{stable_id}:reveal';
export const COPY_PATH_TITLE = 'GET /api/v1/tracks/{stable_id}';
export const REANALYZE_TITLE =
	'POST /api/v1/analysis/backfill/enqueue (`python -m apps.analysis.queue_cli enqueue --lane beatgrid --backend own_beatgrid.backfill`)';

export type ToastFn = (
	message: string,
	kind: 'info' | 'warn' | 'error',
	duration?: number
) => void;

export function loadDeckTitle(deck: number, stableId: string): string {
	return `opendj load ${deck} ${stableId}`;
}

/**
 * The menu a row may show. A stick row (Play from USB) is read only and has
 * no library row behind it, so every item that calls a library-only API (add
 * to playlist, edit, relocate, reveal, copy path, re-analyze, stem / lyrics
 * jobs, remove) is dropped rather than left enabled to be refused by the
 * server. Deck loads are the stick row's real actions. An allowlist, so an
 * item added to the menu later stays off stick rows until it is proven to
 * work for them.
 */
export function menuItemsForTrack<T extends { id: string }>(stableId: string, items: readonly T[]): T[] {
	return isUsbTrackId(stableId) ? items.filter((item) => item.id.startsWith('load-')) : [...items];
}

export function menuTargetIds(
	row: { stable_id: string; order: number },
	selectedOrders: readonly number[],
	selectedIds: readonly string[]
): string[] {
	return selectedOrders.includes(row.order) ? [...selectedIds] : [row.stable_id];
}

export function isStreamingUri(path: string | null | undefined): boolean {
	if (!path) return true;
	const lower = path.toLowerCase();
	return (
		lower.startsWith('tidal:') ||
		lower.startsWith('spotify:') ||
		lower.startsWith('soundcloud:')
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
		deps.pushToast(
			`no file_path on GET /api/v1/tracks/${firstMissingId ?? targetIds[0]}`,
			'error'
		);
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
		deps.pushToast(
			`Revealed ${revealed} track${revealed === 1 ? '' : 's'}`,
			'info'
		);
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
	push(
		presentation.message,
		kind,
		undefined,
		undefined,
		{},
		REANALYZE_TOAST_GROUP,
		undefined,
		{ headline, detail, feature: 'Analysis' }
	);
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
			note: 'track context menu'
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
				onError: (message) => deps.pushToast(message, 'error', undefined, undefined, {}, REANALYZE_TOAST_GROUP)
			});
		}
	} catch (error) {
		deps.pushToast(error instanceof Error ? error.message : String(error), 'error');
	}
}
