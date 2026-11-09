/**
 * Pure helpers for mixer dial and stem lane context menu actions (#9450).
 */
import { ANIMATE_CONTROLS, type AnimateControl } from '../rb/animate-expr';
import type { DeckAutoGain } from '../rb/auto-gain';
import { STEM_CONTROL_IDS, type StemControl } from '../rb/stem-types';

export type DeckId = 1 | 2 | 3 | 4;
export type MixerControlClass = 'eq' | 'trim' | 'filter' | 'fader' | 'crossfader' | 'stem';
export type ControlValue = 'default' | 'cut' | 'other';
/** The deck's auto gain as a menu fact: none, auto (analysed), or user (own offset). */
export type AutoGainFact = 'none' | 'auto' | 'user';

const EQ_CONTROLS = new Set<AnimateControl>(['eq_low', 'eq_mid', 'eq_high']);

export function controlClassOf(control: AnimateControl): MixerControlClass {
	if (control.startsWith('eq_')) return 'eq';
	if (control.startsWith('stem_')) return 'stem';
	if (control === 'trim') return 'trim';
	if (control === 'filter') return 'filter';
	if (control === 'fader') return 'fader';
	return 'crossfader';
}

/** EQ/TRIM/FILTER/crossfader/stem gain default 0.5; channel fader default 1 (full). */
export function controlDefault(control: AnimateControl): number {
	return control === 'fader' ? 1 : 0.5;
}

export function controlResetCommand(
	control: AnimateControl,
	deck: DeckId | null
): Record<string, unknown> {
	return ANIMATE_CONTROLS[control].command(deck ?? 1, controlDefault(control));
}

export function controlKillCommand(control: AnimateControl, deck: DeckId): Record<string, unknown> {
	if (!EQ_CONTROLS.has(control)) throw new RangeError(`Kill is only for EQ bands, not ${control}`);
	const band = control.slice('eq_'.length) as 'low' | 'mid' | 'high';
	return { type: 'eq', deck, band, value: 0 };
}

export function controlValueOf(control: AnimateControl, value: number): ControlValue {
	const def = controlDefault(control);
	if (Math.abs(value - def) < 1e-6) return 'default';
	if (EQ_CONTROLS.has(control) && Math.abs(value) < 1e-6) return 'cut';
	return 'other';
}

export function autoGainFactOf(state: DeckAutoGain): AutoGainFact {
	if (state.state !== 'ready') return 'none';
	if (state.value.status === 'user') return 'user';
	if (state.value.status === 'analysed' && state.value.enabled) return 'auto';
	return 'none';
}

export function autoGainResetReason(fact: AutoGainFact, trimUnity: boolean): string | null {
	if (fact === 'none')
		return 'no auto gain on this deck: no track, auto gain off, or no loudness analysis';
	if (fact === 'auto' && trimUnity) return 'trim already follows auto gain';
	return null;
}

export function trimAutoGainCommands(
	deck: DeckId,
	fact: AutoGainFact,
	trimUnity: boolean,
	stableId: string | null
): Record<string, unknown>[] {
	if (autoGainResetReason(fact, trimUnity) !== null) return [];
	const cmds: Record<string, unknown>[] = [];
	if (!trimUnity) cmds.push({ type: 'trim', deck, value: 0.5 });
	if (fact === 'user' && stableId !== null) {
		cmds.push({ type: 'auto_gain_offset', deck, offset_db: null, stable_id: stableId });
	}
	return cmds;
}

export const STEM_NAMES: Readonly<Record<StemControl, string>> = {
	vocal: 'vocals',
	instrumental: 'instrumental',
	drums: 'drums',
	bass: 'bass',
	other: 'harmony'
};

export function stemLevelsResetCommands(
	deck: DeckId,
	available: readonly StemControl[]
): Record<string, unknown>[] {
	return available.map((stem) => ({ type: 'stem_gain', deck, stem, value: 0.5 }));
}

export function stemLaneActionReason(
	loaded: boolean,
	stems: 'none' | 'ready',
	available: boolean,
	deck: DeckId,
	stem: StemControl
): string | null {
	if (!loaded) return `CH${deck} is empty`;
	if (stems !== 'ready') return `no stems on CH${deck} yet - use Generate stems`;
	if (!available) return `${STEM_NAMES[stem]} is not a separate stem in this bundle`;
	return null;
}
