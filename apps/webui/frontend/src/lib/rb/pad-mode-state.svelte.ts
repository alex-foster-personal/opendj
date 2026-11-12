/**
 * Per-deck selected pad mode (PADFX-11718). Session only, not persisted.
 * Selection reaches this store only through the typed `pad_mode` command
 * (performance-ipc); PadModeMenu twins dispatch, they do not call selectPadMode.
 * A mode is selectable when it is built and, for Pad FX, when the beat FX
 * unit is available on that deck (its refusal reason otherwise).
 */
import type { DeckId } from './deck-id';
import { beatFxUnitAvailability } from './beat-fx-unit-port.svelte';
import { PAD_MODE_CATALOG, type PadModeEntry, type PadModeSnapshot } from './pad-mode-catalog';
import { isPadFxModeId } from './pad-fx-presets';
import { plannedTitle } from './planned-explainers';

export type PadModeSelectable = { enabled: true } | { enabled: false; reason: string };

export const DEFAULT_PAD_MODE = 'hot-cue';

const modes: Record<DeckId, string> = $state({
	1: DEFAULT_PAD_MODE,
	2: DEFAULT_PAD_MODE,
	3: DEFAULT_PAD_MODE,
	4: DEFAULT_PAD_MODE
});

export function padModeFor(deckId: DeckId): string {
	return modes[deckId];
}

export function padModeSelectable(deckId: DeckId, entry: PadModeEntry): PadModeSelectable {
	if (!entry.built) return { enabled: false, reason: plannedTitle('pad-mode-unbuilt') };
	if (isPadFxModeId(entry.id)) {
		const unit = beatFxUnitAvailability(String(deckId) as `${DeckId}`);
		return unit.enabled ? { enabled: true } : { enabled: false, reason: unit.reason };
	}
	return { enabled: true };
}

/** Select a mode for a deck; throws with the reason when it is not selectable. */
export function selectPadMode(deckId: DeckId, modeId: string): void {
	const entry = PAD_MODE_CATALOG.find((e) => e.id === modeId);
	if (entry === undefined) throw new Error(`unknown pad mode ${modeId}`);
	const selectable = padModeSelectable(deckId, entry);
	if (!selectable.enabled) throw new Error(`${entry.label}: ${selectable.reason}`);
	modes[deckId] = modeId;
}

/** Beat Jump pad mode (DECKUX-9956) name for selectPadMode. */
export const setPadMode = selectPadMode;

export function padModeSnapshot(deckId: DeckId): PadModeSnapshot {
	return {
		selected: modes[deckId],
		modes: PAD_MODE_CATALOG.map((entry) => {
			const selectable = padModeSelectable(deckId, entry);
			return {
				id: entry.id,
				label: entry.label,
				built: entry.built,
				enabled: selectable.enabled,
				reason: selectable.enabled ? null : selectable.reason
			};
		})
	};
}
