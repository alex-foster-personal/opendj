/**
 * Session state for library track drags, plus the shared accept/read helpers
 * every drop target uses (decks, playlist rows).
 *
 * WEBKIT: acceptance must NEVER be gated on `dataTransfer.types`. WKWebView
 * (the packaged shell) does not expose custom MIME types in `types` during
 * dragover, so a types gate means preventDefault() never runs, the element
 * never becomes a valid drop target, and ondrop never fires at all. The in-app
 * drag state is the source of truth; the custom MIME is still set on dragstart
 * for cross-app interop and is still preferred when reading a drop.
 */

import type { DraggedDeckRow } from './deck-track-drop';

export { applyDeckTrackDrop } from './deck-track-drop';

export const TRACK_STABLE_MIME = 'application/x-mdt-stable-id';

export const trackDrag = $state<{
	active: boolean;
	stableIds: string[];
	rows: Record<string, DraggedDeckRow>;
}>({
	active: false,
	stableIds: [],
	rows: {}
});

let _endBound = false;

function _onDragEnd(): void {
	endTrackDrag();
}

function _ensureEndListener(): void {
	if (_endBound || typeof window === 'undefined') return;
	window.addEventListener('dragend', _onDragEnd, true);
	_endBound = true;
}

export function beginTrackDrag(
	stableIds: string[],
	rows: Record<string, DraggedDeckRow> = {}
): void {
	const ids = stableIds.map((s) => s.trim()).filter((s) => s.length > 0);
	if (ids.length === 0) return;
	trackDrag.active = true;
	trackDrag.stableIds = ids;
	trackDrag.rows = rows;
	_ensureEndListener();
}

export function endTrackDrag(): void {
	trackDrag.active = false;
	trackDrag.stableIds = [];
	trackDrag.rows = {};
}

export function droppedRowFlags(stableId: string): DraggedDeckRow | null {
	return trackDrag.rows[stableId] ?? null;
}

/**
 * Mark a dragover as a droppable track drag. Returns true when accepted, so a
 * caller can set its own hover state without repeating the condition.
 */
export function acceptTrackDragOver(event: DragEvent): boolean {
	if (!trackDrag.active) return false;
	event.preventDefault();
	if (event.dataTransfer !== null) event.dataTransfer.dropEffect = 'copy';
	return true;
}

/** LIBM-84 (issue #12307): why a smartlist refuses a track drop. */
export const SMARTLIST_DROP_REFUSAL =
	'Smartlists fill themselves from their rules, so tracks cannot be dropped into one';

/**
 * Why a tree node of this kind refuses a track drop, or null when it takes
 * one. Only a smartlist is refused out loud; other non-playlist kinds stay
 * silent non-targets as before.
 */
export function trackDropRefusal(kind: string): string | null {
	return kind === 'smartlist' ? SMARTLIST_DROP_REFUSAL : null;
}

/**
 * Mark a dragover on a node that refuses tracks (LIBM-84). Shows the no-drop
 * cursor and never calls preventDefault, so the drop event never fires and no
 * playlist request can follow. Returns true when a track drag was refused.
 */
export function refuseTrackDragOver(event: DragEvent): boolean {
	if (!trackDrag.active) return false;
	if (event.dataTransfer !== null) event.dataTransfer.dropEffect = 'none';
	return true;
}

/**
 * Stable ids carried by a drop: the explicit transfer payload first (it is the
 * only thing a cross-app or cross-window drag can carry), the in-app drag state
 * second (WebKit's protected drag mode can hand back an empty getData).
 */
export function droppedStableIds(event: DragEvent): string[] {
	const fromTransfer = (event.dataTransfer?.getData(TRACK_STABLE_MIME) ?? '')
		.split(',')
		.map((id) => id.trim())
		.filter((id) => id !== '');
	if (fromTransfer.length > 0) return fromTransfer;
	return [...trackDrag.stableIds];
}

export function primaryDroppedStableId(event: DragEvent): string | null {
	return droppedStableIds(event)[0] ?? null;
}
