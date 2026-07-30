/**
 * Session state for library track drags (Computer-As-Decks drop targets).
 * Sources call begin/end; DeckLoadDropOverlay reads `trackDrag.active`.
 */

export const TRACK_STABLE_MIME = 'application/x-mdt-stable-id';

export const trackDrag = $state<{
	active: boolean;
	stableIds: string[];
}>({
	active: false,
	stableIds: []
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

export function beginTrackDrag(stableIds: string[]): void {
	const ids = stableIds.map((s) => s.trim()).filter((s) => s.length > 0);
	if (ids.length === 0) return;
	trackDrag.active = true;
	trackDrag.stableIds = ids;
	_ensureEndListener();
}

export function endTrackDrag(): void {
	trackDrag.active = false;
	trackDrag.stableIds = [];
}

export function primaryDragStableId(): string | null {
	return trackDrag.stableIds[0] ?? null;
}
