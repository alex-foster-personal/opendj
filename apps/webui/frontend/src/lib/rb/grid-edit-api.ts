/**
 * The deck grid buttons' engine calls (GRIDEDIT-6235c, -6235d): the SAME routes an
 * agent and `opendj track grid-edit` use, through the typed client, plus the
 * one re-read that moves every deck holding the track onto the edited grid.
 * The two HTTP calls live in beatgrid-fallback-api.ts, the other per-track
 * beatgrid client (not `$lib/api` or a new client importer: both fan-ins are
 * ratcheted).
 *
 * `runGridEdit` is the `grid_edit` performance command's body: the deck
 * buttons and an agent both reach it through the shared dispatcher in
 * performance-ipc.svelte.ts (via grid-edit-command), never directly.
 */

import { refetchAnlzCacheEntry } from '$lib/components/rb/wave/anlz-cache.svelte';
import { gridEditUnavailableReason, type AnlzData, type GridEditIntent } from './grid-edit';
import {
	createEventRefresher,
	createGridEditQueue,
	type EventRefresher,
	type GridEditPorts,
	type GridEditQueue
} from './grid-edit-queue';
import { nudgeGridEdit, resetGridEditPart } from './beatgrid-fallback-api';
import { isUsbTrackId } from './track-source';

/** Re-read the track's `/anlz` so every deck holding it moves onto the edited
 * grid (the same path a hot-cue save and a PARITY-02 source switch take). */
export const refreshTrackGrid = refetchAnlzCacheEntry;

export function gridEditPorts(): GridEditPorts {
	return {
		nudge: nudgeGridEdit,
		reset: resetGridEditPart,
		refresh: refreshTrackGrid
	};
}

// One queue for every deck: two decks holding the same track edit one stored
// grid, so their presses must serialize against each other.
let _queue: GridEditQueue | null = null;

export function gridEditQueue(): GridEditQueue {
	_queue ??= createGridEditQueue(gridEditPorts());
	return _queue;
}

/** The deck buttons' re-read on a `beatgrid_edit` event or a resync, one per
 * page, behind the same queue as the presses (GridEditButtons loads it lazily). */
export function gridEditEventRefresher(reportError: (message: string) => void): EventRefresher {
	return createEventRefresher(gridEditQueue(), refreshTrackGrid, reportError);
}

/** Applies one press to the grid of the track a deck holds; rejects with why
 * the deck cannot be edited, or with the engine's refusal. */
export async function runGridEdit(
	stableId: string | null,
	anlz: AnlzData | null,
	intent: GridEditIntent,
	guard?: () => void
): Promise<void> {
	const unavailable = gridEditUnavailableReason(
		stableId, anlz, stableId !== null && isUsbTrackId(stableId)
	);
	if (unavailable !== null || stableId === null || anlz === null) {
		throw new Error(`grid edit: ${unavailable ?? 'deck not ready'}`);
	}
	await gridEditQueue().submit(stableId, anlz.beatgrid.source, intent, guard);
}
