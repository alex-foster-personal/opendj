/** STEMPERF-11695: one-press stem quick modes for a deck. ACA plays the
 * acapella (VOCAL alone), INST the instrumental (VOCAL muted, no solo) and
 * ALL goes back to everything (no mute, no solo; gains untouched). Pure and
 * read-model only: a mode is a minimal plan over the existing `stem_solo` /
 * `stem_mute` commands, so the engine and the command surface stay as they
 * are. Exported so a CLI verb can reuse the same plan later.
 *
 * The issue sketches `quickModePlan(mode, layout, controls)`; commands need a
 * deck and the layout reaches the plan as `available_controls`, hence
 * `quickModePlan(deck, mode, stems)`. */
import type { DeckId } from './deck-id';
import type { PerformanceCommand } from './performance-ipc.svelte';
import { STEM_CONTROL_IDS, type StemControl, type StemDeckState } from './stem-types';

export type StemQuickMode = 'aca' | 'inst' | 'all';
export type StemQuickState = StemQuickMode | 'custom';
export const STEM_QUICK_MODES: readonly StemQuickMode[] = ['aca', 'inst', 'all'];
export type QuickStems = Pick<StemDeckState, 'available_controls' | 'controls'>;
export type StemQuickCommand = Extract<PerformanceCommand, { type: 'stem_solo' | 'stem_mute' }>;

//-----------------------------------------------------------------------------
// helpers

/** The layout's controls in canonical order; never a control the layout lacks. */
function _live(stems: QuickStems): StemControl[] {
	return STEM_CONTROL_IDS.filter((id) => stems.available_controls.includes(id));
}

function _solos(stems: QuickStems): StemControl[] {
	return _live(stems).filter((id) => stems.controls[id].solo);
}

function _mutes(stems: QuickStems): StemControl[] {
	return _live(stems).filter((id) => stems.controls[id].muted);
}

function _requireVocal(deck: DeckId, mode: StemQuickMode, stems: QuickStems): void {
	if (!stems.available_controls.includes('vocal'))
		throw new Error(`deck ${deck} has no vocal stem for quick mode ${mode}`);
}

const _unsolo = (deck: DeckId, stem: StemControl): StemQuickCommand => ({
	type: 'stem_solo',
	deck,
	stem,
	solo: false
});
const _mute = (deck: DeckId, stem: StemControl, muted: boolean): StemQuickCommand => ({
	type: 'stem_mute',
	deck,
	stem,
	muted
});

//-----------------------------------------------------------------------------
// public

/** The commands that take the deck from its current controls to `mode`:
 * solo commands first, then mute commands, each in STEM_CONTROL_IDS order,
 * one only for a control that must change. */
export function quickModePlan(
	deck: DeckId,
	mode: StemQuickMode,
	stems: QuickStems
): StemQuickCommand[] {
	const solos = _solos(stems);
	if (mode === 'aca') {
		_requireVocal(deck, mode, stems);
		const plan: StemQuickCommand[] = [];
		if (!(solos.length === 1 && solos[0] === 'vocal')) {
			plan.push({ type: 'stem_solo', deck, stem: 'vocal', solo: true, exclusive: true });
		}
		if (stems.controls.vocal.muted) plan.push(_mute(deck, 'vocal', false));
		return plan;
	} else if (mode === 'inst') {
		_requireVocal(deck, mode, stems);
		const remute = _live(stems).filter((id) => stems.controls[id].muted !== (id === 'vocal'));
		return [
			...solos.map((id) => _unsolo(deck, id)),
			...remute.map((id) => _mute(deck, id, id === 'vocal'))
		];
	} else if (mode === 'all') {
		return [
			...solos.map((id) => _unsolo(deck, id)),
			..._mutes(stems).map((id) => _mute(deck, id, false))
		];
	} else {
		const unhandled: never = mode;
		throw new Error(`Unhandled stem quick mode: ${String(unhandled)}`);
	}
}

/** Which quick mode the current controls equal, from the read model alone. */
export function quickModeActive(stems: QuickStems): StemQuickState {
	const solos = _solos(stems);
	const mutes = _mutes(stems);
	if (solos.length === 0 && mutes.length === 0) return 'all';
	else if (solos.length === 1 && solos[0] === 'vocal' && !stems.controls.vocal.muted) return 'aca';
	else if (solos.length === 0 && mutes.length === 1 && mutes[0] === 'vocal') return 'inst';
	else return 'custom';
}
