/**
 * Keyboard selector ids for the mixer's lower controls (GitHub issue #13706, epic #9372).
 *
 * Each crossfader assign matrix is a group (`mixer.assign.a` / `mixer.assign.b`) of four
 * channel leaves, and the crossfade curve select and the group pad are leaves, all children
 * of the mixer group. Ids are plain strings composed from `selId.mixer`, so
 * `selector-register.ts` gains no new helper.
 */
import { selId } from '$lib/keyboard/selector-register';
import type { DeckId } from '$lib/rb/deck-slots';

export type AssignBus = 'A' | 'B';

/** Returns the selector group id of the crossfader assign matrix for one bus. */
export function assignMatrixSelectorId(bus: AssignBus): string {
	return `${selId.mixer()}.assign.${bus.toLowerCase()}`;
}

/** Returns the selector leaf id of one channel's switch inside a bus's assign matrix. */
export function assignSwitchSelectorId(bus: AssignBus, deck: DeckId): string {
	return `${assignMatrixSelectorId(bus)}.ch${deck}`;
}

/** Returns the selector leaf id of the crossfade curve select. */
export function crossfadeCurveSelectorId(): string {
	return `${selId.mixer()}.crossfade-curve`;
}

/** Returns the selector leaf id of the two-axis group pad. */
export function groupPadSelectorId(): string {
	return `${selId.mixer()}.group-pad`;
}
