/**
 * AutoPlay recovery after an honest silence stop (issue #2069).
 */

import type { AutoPlayDeckSnap } from '$lib/rb/auto-play';

export type AutoPlaySilenceRecoveryPlan =
	| { action: 'idle-ok' }
	| { action: 'recover' }
	| { action: 'disarm-ok' };

let _recoveringFromSilence = false;

export function planAutoPlaySilenceRecovery(input: {
	enabled: boolean;
	has_playable_next: boolean;
	stall_active: boolean;
}): AutoPlaySilenceRecoveryPlan {
	if (!input.enabled) return { action: 'idle-ok' };
	if (input.has_playable_next) return { action: 'recover' };
	return { action: 'disarm-ok' };
}

export function noteAutoPlaySilenceDropout(input: { has_playable_next: boolean }): void {
	if (input.has_playable_next) _recoveringFromSilence = true;
}

export function isSilenceRecovering(): boolean {
	return _recoveringFromSilence;
}

export function clearSilenceRecovery(): void {
	_recoveringFromSilence = false;
}

export function pickPausedMasterSource(
	snaps: readonly AutoPlayDeckSnap[]
): AutoPlayDeckSnap | null {
	return snaps.find((deck) => deck.is_master && deck.stable_id !== null) ?? null;
}

export function resolveAutoPlaySourceForTick(
	snaps: readonly AutoPlayDeckSnap[],
	pickSource: (decks: readonly AutoPlayDeckSnap[]) => AutoPlayDeckSnap | null
): { source: AutoPlayDeckSnap | null; remainingOverride: number | null } {
	let source = pickSource(snaps);
	if (source !== null) return { source, remainingOverride: null };
	if (!_recoveringFromSilence) return { source: null, remainingOverride: null };
	source = pickPausedMasterSource(snaps);
	if (source === null) {
		_recoveringFromSilence = false;
		return { source: null, remainingOverride: null };
	}
	return { source, remainingOverride: 0 };
}

export function noteAutoPlayFollowerPlayDispatched(): void {
	_recoveringFromSilence = false;
}

export function resetAutoPlaySilenceRecovery(): void {
	_recoveringFromSilence = false;
}
