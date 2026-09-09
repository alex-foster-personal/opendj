/**
 * Pure deck-load/replacement/master-selection guards, split out of
 * audio-engine.svelte.ts (a distinct concern from the engine class itself -
 * none of these read `_ctx`/`_rt`/module-level engine state, they only
 * validate or derive from arguments the engine passes in). Kept as a plain
 * .ts module (no runes) since nothing here is reactive.
 */
// DeckId comes through player/constants (which already re-exports it
// alongside DECK_IDS) rather than a fresh direct import of deck-slots.ts -
// that module sits at its frontend.max_fan_in allowance, and constants.ts's
// own re-export exists for exactly this pairing.
import { DECK_IDS, type DeckId } from '$lib/player/constants';
import type { DeckState } from '$lib/rb/deck-state-types';
import type { PresentedTransportObservation } from '$lib/player/transport/presentation';

export function nextPlayingMaster(playingDecks: readonly DeckId[]): DeckId | null {
	return DECK_IDS.find((deck) => playingDecks.includes(deck)) ?? null;
}

export function assertDeckLoadConsistency(
	stableId: string | null,
	durationSec: number,
	hasProcessor: boolean
): void {
	const stateLoaded = stableId !== null;
	const runtimeLoaded = hasProcessor && durationSec > 0;
	if (stateLoaded !== runtimeLoaded) {
		throw new Error(
			`inconsistent deck load state: stable_id=${String(stableId)}, ` +
				`duration=${durationSec}, processor=${hasProcessor}`
		);
	}
}

export interface DeckReplacementActivity {
	playing: boolean;
	audible: boolean;
	transportPending: boolean;
	controlActive: boolean;
	pendingScheduleCount: number;
	scheduleIntentCount: number;
}

export function assertDeckReplacementAllowed(
	deck: DeckId,
	activity: DeckReplacementActivity
): void {
	for (const [name, value] of Object.entries({
		playing: activity.playing,
		audible: activity.audible,
		transportPending: activity.transportPending,
		controlActive: activity.controlActive
	})) {
		if (typeof value !== 'boolean') {
			throw new TypeError(`${name} must be boolean, got ${String(value)}`);
		}
	}
	for (const [name, value] of Object.entries({
		pendingScheduleCount: activity.pendingScheduleCount,
		scheduleIntentCount: activity.scheduleIntentCount
	})) {
		if (!Number.isInteger(value) || value < 0) {
			throw new RangeError(`${name} must be a non-negative integer, got ${value}`);
		}
	}
	const activeReasons = Object.entries(activity)
		.filter(([, value]) => value === true || (typeof value === 'number' && value > 0))
		.map(([name]) => name);
	if (activeReasons.length === 0) return;
	throw new Error(
		`load: deck ${deck} must be fully stopped before replacement; active state: ` +
			activeReasons.join(', ')
	);
}

export function loadCandidateCanPublish(candidateToken: number, currentToken: number): boolean {
	for (const [name, value] of Object.entries({ candidateToken, currentToken })) {
		if (!Number.isInteger(value) || value <= 0) {
			throw new RangeError(`${name} must be a positive integer, got ${value}`);
		}
	}
	return candidateToken === currentToken;
}

export function assertPausedMasterSelectionAllowed(
	deck: DeckId,
	audible: boolean,
	otherActiveDecks: readonly DeckId[]
): void {
	if (audible || otherActiveDecks.length === 0) return;
	throw new Error(
		`setDeckMaster: cannot select paused deck ${deck} while decks ` +
			`[${otherActiveDecks.join(',')}] are audible or scheduled to play`
	);
}

export function pausedMasterSelectionBlockers(
	deck: DeckId,
	activity: Readonly<Record<DeckId, Pick<DeckState, 'audible' | 'playing'>>>
): DeckId[] {
	return DECK_IDS.filter(
		(candidate) =>
			candidate !== deck && (activity[candidate].audible || activity[candidate].playing)
	);
}

export function masterSwitchFollowers(
	deck: DeckId,
	activity: Readonly<Record<DeckId, Pick<DeckState, 'playing' | 'beat_sync_enabled'>>>
): DeckId[] {
	return DECK_IDS.filter(
		(candidate) =>
			candidate !== deck &&
			activity[candidate].playing &&
			activity[candidate].beat_sync_enabled
	);
}

export function naturalEndNeedsRevisionedStop(
	playing: boolean,
	observation: Pick<
		PresentedTransportObservation,
		'audible' | 'transport_pending' | 'position_sec'
	>,
	durationSec: number,
	scheduleIntentCount: number
): boolean {
	if (typeof playing !== 'boolean') {
		throw new TypeError(`playing must be boolean, got ${String(playing)}`);
	}
	if (!Number.isFinite(durationSec) || durationSec <= 0) {
		throw new RangeError(`durationSec must be finite and positive, got ${durationSec}`);
	}
	if (
		typeof observation.audible !== 'boolean' ||
		typeof observation.transport_pending !== 'boolean'
	) {
		throw new TypeError('natural-end observation flags must be boolean');
	}
	if (!Number.isFinite(observation.position_sec) || observation.position_sec < 0) {
		throw new RangeError(
			`natural-end position must be finite and non-negative, got ${observation.position_sec}`
		);
	}
	if (!Number.isInteger(scheduleIntentCount) || scheduleIntentCount < 0) {
		throw new RangeError(
			`scheduleIntentCount must be a non-negative integer, got ${scheduleIntentCount}`
		);
	}
	return (
		playing &&
		!observation.audible &&
		!observation.transport_pending &&
		scheduleIntentCount === 0 &&
		observation.position_sec >= durationSec
	);
}

export interface TransportMutationActivity {
	playing: boolean;
	audible: boolean;
	controlActive: boolean;
	pendingScheduleCount: number;
	scheduleIntentCount: number;
	/** Presentation clock lag: desired_revision !== presented_revision.
	 * The control clock (`pendingScheduleCount`) drains off `ctx.currentTime`,
	 * but the presentation clock only advances inside the rAF tick. When rAF
	 * stops - a hidden tab, or a natural end that left nothing audible to
	 * animate - the control clock reaches idle while presentation still lags.
	 * Without this flag a seek takes the paused-cursor branch, which then
	 * throws in setPausedTransportTimelineCursor and wedges the deck. */
	presentationPending: boolean;
}

export function transportNeedsScheduledMutation(
	activity: TransportMutationActivity
): boolean {
	for (const [name, value] of Object.entries({
		playing: activity.playing,
		audible: activity.audible,
		controlActive: activity.controlActive,
		presentationPending: activity.presentationPending
	})) {
		if (typeof value !== 'boolean') {
			throw new TypeError(`${name} must be boolean, got ${String(value)}`);
		}
	}
	for (const [name, value] of Object.entries({
		pendingScheduleCount: activity.pendingScheduleCount,
		scheduleIntentCount: activity.scheduleIntentCount
	})) {
		if (!Number.isInteger(value) || value < 0) {
			throw new RangeError(`${name} must be a non-negative integer, got ${value}`);
		}
	}
	return (
		activity.playing ||
		activity.audible ||
		activity.controlActive ||
		activity.presentationPending ||
		activity.pendingScheduleCount > 0 ||
		activity.scheduleIntentCount > 0
	);
}

// Deck-load/replacement/master-selection guards moved to
