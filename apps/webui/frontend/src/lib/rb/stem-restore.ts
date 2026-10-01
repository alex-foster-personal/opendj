/** Shared restore policy. Supersedes eager stem dispatch in session/rescue
 * restore: mix decoding completes before the secondary stem graph is ready. */
import type { DeckId } from './deck-id';
import type { PerformanceCommand, PerformanceState } from './performance-ipc.svelte';
import { STEM_CONTROL_IDS, type StemControl, type StemControlState } from './stem-types';

/** How long a restore waits for a deck's stems to leave `loading`.
 *
 * On an installed spoke the bundle may still be downloading from R2, and the
 * deck itself keeps `loading` for up to STEM_HYDRATE_MAX_WAIT_MS (ten minutes,
 * stem-hydrate-wait.ts, STEM-37) before it settles `error`. This must outlive
 * that wait, so the deck's own verdict ends the restore: a shorter deadline
 * dropped saved mute / solo / gain for stems that became ready moments later.
 * The extra minute covers decoding after the download lands. Kept as a literal
 * so this module stays free of the engine-only hydrate module; the unit test
 * pins the ordering between the two. */
export const STEM_RESTORE_MAX_WAIT_MS = 11 * 60 * 1000;
const STEM_RESTORE_POLL_MS = 25;

export type RestoreStemControlsClock = {
	now: () => number;
	sleep: (ms: number) => Promise<void>;
};

const _wallClock: RestoreStemControlsClock = {
	now: () => performance.now(),
	sleep: (ms) => new Promise<void>((resolve) => setTimeout(resolve, ms))
};

export async function restoreStemControls(
	dispatch: (command: PerformanceCommand) => Promise<PerformanceState>,
	query: () => PerformanceState,
	deck: DeckId,
	controls: Partial<Record<StemControl, Omit<StemControlState, 'gain'> & { gain?: number }>>,
	clock: RestoreStemControlsClock = _wallClock
): Promise<void> {
	const initial = query().decks[deck];
	const deadline = clock.now() + STEM_RESTORE_MAX_WAIT_MS;
	const current = () => {
		const state = query().decks[deck];
		if (state.load_generation !== initial.load_generation || state.stable_id !== initial.stable_id) {
			throw new Error(`deck ${deck} changed during stem restore`);
		}
		return state.stems;
	};
	while (current().status === 'loading') {
		if (clock.now() >= deadline) throw new Error(`deck ${deck} stem restore timed out`);
		await clock.sleep(STEM_RESTORE_POLL_MS);
	}
	const stems = current();
	if (stems.status === 'error') throw new Error(`deck ${deck} stem restore failed: ${stems.error}`);
	for (const stem of STEM_CONTROL_IDS) {
		const control = controls[stem];
		if (control === undefined) continue;
		if (!stems.available_controls.includes(stem)) {
			// Legacy snapshots include neutral DRUMS even for two-part/no-stem tracks.
			// A real saved effect must never disappear silently when capability is lost.
			if (control.muted || control.solo || (control.gain !== undefined && control.gain !== 0.5)) {
				throw new Error(`deck ${deck} cannot restore unavailable ${stem} control`);
			}
			continue;
		}
		const commands: PerformanceCommand[] = [
			{ type: 'stem_mute', deck, stem, muted: control.muted },
			{ type: 'stem_solo', deck, stem, solo: control.solo }
		];
		if (control.gain !== undefined) commands.push({ type: 'stem_gain', deck, stem, value: control.gain });
		for (const command of commands) {
			current();
			await dispatch(command);
		}
	}
}
