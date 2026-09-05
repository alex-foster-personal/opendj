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
