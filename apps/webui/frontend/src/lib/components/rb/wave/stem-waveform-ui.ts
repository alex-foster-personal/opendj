/** Pure helpers for stem mini-waveform rows (issue #1036). */

import type { DeckState } from '$lib/rb/deck-state-types';
import { stemColorFromToken } from '$lib/rb/stem-colors';
import { STEM_CONTROLS } from '$lib/rb/stem-graph';
import type { StemControl, StemLayout } from '$lib/rb/stem-types';
import type { PerformanceCommand } from '$lib/rb/performance-ipc-types';

/** Fixed CSS height for one stem mini-waveform row. */
export const STEM_WAVE_ROW_PX = 12;

/** Maximum stem rows shown under one deck when ``show_stems`` is on. */
export const STEM_WAVE_ROW_MAX = STEM_CONTROLS.length;

export function stemWaveformApiPart(
	control: StemControl,
	layout: StemLayout | null
): string | null {
	if (control === 'vocal') return 'vocals';
	if (control === 'instrumental') return 'instrumental';
	// drums / bass / other are real parts of a demucs4 bundle only; the
	// waveform route names them as the bundle does.
	return layout === 'demucs4' ? control : null;
}

export function stemWaveRowUnavailableTip(
	deck: Pick<DeckState, 'stems'>,
	stem: StemControl
): string | null {
	if (deck.stems.status !== 'ready') {
		return `stems ${deck.stems.status}: ${deck.stems.error ?? 'no aligned artifact'}`;
	}
	if (!deck.stems.available_controls.includes(stem)) {
		const label = stem === 'vocal' ? 'VOCAL' : stem === 'instrumental' ? 'INST' : 'DRUMS';
		return (
			`${label} is not a separate stem in this ${deck.stems.layout ?? 'bundle'} ` +
			`- it is mixed into INST, so it cannot be muted on its own`
		);
	}
	return null;
}

/** Canvas colour for a stem row: the skin's --rb-stem-<id> computed value, else the default. */
export function stemWaveRowColor(stem: StemControl, computedToken = ''): string {
	return stemColorFromToken(stem, computedToken);
}

/**
 * Resolve a stem row colour from the element's --rb-stem-<id> token. Forces a
 * style read, so callers run it once per theme/palette/skin/stem change, never
 * per paint (issue #10221, PERF-PAINT-02).
 */
export function stemWaveRowColorFromElement(el: Element, stem: StemControl): string {
	return stemWaveRowColor(stem, getComputedStyle(el).getPropertyValue(`--rb-stem-${stem}`));
}

/** The command a click on a stem mini-waveform row sends (issue #10317). */
export type StemWaveRowCommand = Extract<PerformanceCommand, { type: 'stem_solo' | 'stem_mute' }>;

/**
 * What a click (solo, exclusive, toggling the current solo) or a Shift+click
 * (mute, toggling the current mute) on a stem row sends; null for a row the
 * stem-set's layout cannot drive, so an inert row never dispatches.
 */
export function stemWaveRowClickCommand(
	deck: Pick<DeckState, 'deck_id' | 'stems'>,
	stem: StemControl,
	shiftKey: boolean
): StemWaveRowCommand | null {
	if (stemWaveRowUnavailableTip(deck, stem) !== null) return null;
	const current = deck.stems.controls[stem];
	return shiftKey
		? { type: 'stem_mute', deck: deck.deck_id, stem, muted: !current.muted }
		: { type: 'stem_solo', deck: deck.deck_id, stem, solo: !current.solo, exclusive: true };
}
