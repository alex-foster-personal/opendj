/** Shared restore policy. Supersedes eager stem dispatch in session/rescue
 * restore: mix decoding completes before the secondary stem graph is ready. */
import type { DeckId } from './deck-id';
import type { PerformanceCommand, PerformanceState } from './performance-ipc.svelte';
import { STEM_CONTROL_IDS, type StemControl, type StemControlState } from './stem-types';

export async function restoreStemControls(
	dispatch: (command: PerformanceCommand) => Promise<PerformanceState>,
	query: () => PerformanceState,
	deck: DeckId,
	controls: Partial<Record<StemControl, Omit<StemControlState, 'gain'> & { gain?: number }>>
): Promise<void> {
	const initial = query().decks[deck];
	const deadline = performance.now() + 30_000;
	const current = () => {
		const state = query().decks[deck];
		if (state.load_generation !== initial.load_generation || state.stable_id !== initial.stable_id) {
			throw new Error(`deck ${deck} changed during stem restore`);
		}
		return state.stems;
	};
	while (current().status === 'loading') {
		if (performance.now() >= deadline) throw new Error(`deck ${deck} stem restore timed out`);
		await new Promise<void>((resolve) => setTimeout(resolve, 25));
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
