/**
 * Play from USB (specs/usb-play-from-stick.md 4b "Frontend guards" and
 * decision 2): edits to a stick track stay in this page session. They are
 * never written to the stick and make no request to the daemon.
 *
 * Hot cues: this module stands in for the library's hot cue write routes
 * with the same contract, so performance-ipc and the deck keep calling the
 * same api-rb functions (4b "Routing"). A save or clear must present the
 * slot's current revision (the If-Match check), every change mints a fresh
 * revision, and each save or clear returns a single-use reversal. The
 * stick's own slots come from GET /api/v1/usb/tracks/{id}/hot-cues. A session
 * edit replaces its slot until the page reloads, including across an eject
 * and reload of the same track. Reads apply the edits to both the slot list
 * and the /anlz cue list, so the pads, the waveform markers and the display
 * loop agree.
 *
 * Rating: the deck header's stars set a session rating, and getTrack applies
 * it to the stick's TrackOut.
 *
 * Errors carry status 0 because no HTTP exchange happens. Their codes mirror
 * the library routes (apps/adapters/rekordbox/cues.py, reversal.py).
 */
import { RbApiError } from './api-rb-error';
import type { AnlzCue, HotCueMutation, HotCueSlotState } from './anlz-types';
import type { HotCueSlot } from './hot-cue-types';

interface SlotState {
	cue: AnlzCue | null;
	revision: string;
}

interface SessionReversal {
	stableId: string;
	slot: HotCueSlot;
	/** The revision the reversible change produced (restore requires it). */
	revision: string;
	/** The slot's cue before the change; null for an empty slot. */
	prior: AnlzCue | null;
}

/** The stick's own slots, as the last GET reported them. */
const _stickSlots = new Map<string, Map<HotCueSlot, SlotState>>();
/** This session's edits, by track, then slot. */
const _editedSlots = new Map<string, Map<HotCueSlot, SlotState>>();
const _reversals = new Map<string, SessionReversal>();
const _ratings = new Map<string, number>();
let _sequence = 0;

//-----------------------------------------------------------------------------
// _helpers
//-----------------------------------------------------------------------------

function _nextToken(kind: 'revision' | 'reversal'): string {
	_sequence += 1;
	return `session-${kind}-${_sequence}`;
}

function _refuse(code: string, message: string): never {
	throw new RbApiError(0, code, message);
}

function _currentSlot(stableId: string, slot: HotCueSlot): SlotState {
	const edited = _editedSlots.get(stableId)?.get(slot);
	if (edited !== undefined) return edited;
	const own = _stickSlots.get(stableId)?.get(slot);
	if (own === undefined) {
		throw new Error(`hot cue ${slot}: the stick's own slots for ${stableId} were never read`);
	}
	return own;
}

function _requireRevision(stableId: string, slot: HotCueSlot, revision: string): SlotState {
	const current = _currentSlot(stableId, slot);
	if (revision !== current.revision) {
		_refuse('HOT_CUE_REVISION_CONFLICT', `hot cue ${slot} changed in this session since it was read`);
	}
	return current;
}

function _setSlot(stableId: string, slot: HotCueSlot, cue: AnlzCue | null): string {
	const revision = _nextToken('revision');
	let edits = _editedSlots.get(stableId);
	if (edits === undefined) {
		edits = new Map();
		_editedSlots.set(stableId, edits);
	}
	edits.set(slot, { cue, revision });
	return revision;
}

function _reversibleChange(stableId: string, slot: HotCueSlot, revision: string, cue: AnlzCue | null): HotCueMutation {
	const prior = _requireRevision(stableId, slot, revision).cue;
	const next = _setSlot(stableId, slot, cue);
	const reversal_id = _nextToken('reversal');
	_reversals.set(reversal_id, { stableId, slot, revision: next, prior });
	return { cue, revision: next, reversal: { reversal_id } };
}

//-----------------------------------------------------------------------------
// reads
//-----------------------------------------------------------------------------

/** Records the stick's own slots and returns them with this session's edits. */
export function withSessionHotCueSlots(stableId: string, stickSlots: HotCueSlotState[]): HotCueSlotState[] {
	_stickSlots.set(stableId, new Map(stickSlots.map(({ slot, cue, revision }) => [slot, { cue, revision }])));
	const edits = _editedSlots.get(stableId);
	if (edits === undefined) return stickSlots;
	return stickSlots.map((own) => {
		const edited = edits.get(own.slot);
		return edited === undefined ? own : { slot: own.slot, ...edited };
	});
}

/** The payload with every edited slot's cue replaced by the session's. */
export function withSessionHotCues<T extends { cues: AnlzCue[] }>(stableId: string, anlz: T): T {
	const edits = _editedSlots.get(stableId);
	if (edits === undefined) return anlz;
	const kept = anlz.cues.filter((cue) => cue.slot === null || !edits.has(cue.slot));
	const edited = [...edits.values()].flatMap(({ cue }) => (cue === null ? [] : [cue]));
	return { ...anlz, cues: [...kept, ...edited] };
}

/** The track with the session's rating, when one was set. */
export function withSessionRating<T extends { rating?: number | null }>(stableId: string, track: T): T {
	const rating = _ratings.get(stableId);
	return rating === undefined ? track : { ...track, rating };
}

//-----------------------------------------------------------------------------
// writes (session only)
//-----------------------------------------------------------------------------

/** The library's save semantics (apps/adapters/rekordbox/writer.py
 * save_hot_cue): a point cue at `in_ms`, loop and color cleared. */
export function saveSessionHotCue(
	stableId: string,
	slot: HotCueSlot,
	in_ms: number,
	revision: string,
	comment: string | null
): HotCueMutation {
	if (!Number.isInteger(in_ms) || in_ms < 0) {
		_refuse('HOT_CUE_POSITION_INVALID', `hot cue ${slot}: in_ms must be a whole number >= 0, got ${in_ms}`);
	}
	return _reversibleChange(stableId, slot, revision, {
		kind: 'hot_cue',
		slot,
		in_ms,
		out_ms: null,
		is_loop: false,
		active_loop: false,
		beat_loop_size: null,
		color_table_index: null,
		comment
	});
}

export function clearSessionHotCue(stableId: string, slot: HotCueSlot, revision: string): HotCueMutation {
	return _reversibleChange(stableId, slot, revision, null);
}

/** Consumes one reversal: the slot returns to its cue before that change,
 * under a fresh revision (a revision is never reused, as on the server). */
export function restoreSessionHotCue(
	stableId: string,
	slot: HotCueSlot,
	revision: string,
	reversal_id: string
): HotCueMutation {
	const reversal = _reversals.get(reversal_id);
	if (reversal === undefined) {
		_refuse('HOT_CUE_REVERSAL_NOT_FOUND', `hot cue ${slot}: reversal ${reversal_id} is unknown or already used`);
	}
	if (reversal.stableId !== stableId || reversal.slot !== slot) {
		_refuse('HOT_CUE_REVERSAL_SCOPE_CONFLICT', `hot cue ${slot}: reversal ${reversal_id} belongs to another slot`);
	}
	_requireRevision(stableId, slot, revision);
	if (reversal.revision !== revision) {
		_refuse('HOT_CUE_REVERSAL_STALE', `hot cue ${slot} changed after the reversible change`);
	}
	_reversals.delete(reversal_id);
	return { cue: reversal.prior, revision: _setSlot(stableId, slot, reversal.prior) };
}

export function setSessionRating(stableId: string, rating: number): void {
	if (!Number.isInteger(rating) || rating < 0 || rating > 5) {
		_refuse('RATING_INVALID', `rating must be a whole number from 0 to 5, got ${rating}`);
	}
	_ratings.set(stableId, rating);
}
