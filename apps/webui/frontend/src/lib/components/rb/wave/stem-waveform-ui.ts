/** Pure helpers for stem mini-waveform rows (issue #1036). */

import type { DeckState } from '$lib/rb/deck-state-types';
import { STEM_COLORS } from '$lib/rb/stem-colors';
import { STEM_CONTROLS } from '$lib/rb/stem-graph';
import type { StemControl, StemLayout } from '$lib/rb/stem-types';

/** Fixed CSS height for one stem mini-waveform row. */
export const STEM_WAVE_ROW_PX = 12;

/** Maximum stem rows shown under one deck when ``show_stems`` is on. */
export const STEM_WAVE_ROW_MAX = STEM_CONTROLS.length;

export function stemWaveformApiPart(control: StemControl, layout: StemLayout | null): string | null {
	if (control === 'vocal') return 'vocals';
	if (control === 'instrumental') return 'instrumental';
	// drums / bass / other are real parts of a demucs4 bundle only; the
	// waveform route names them as the bundle does.
	return layout === 'demucs4' ? control : null;
}

export function stemWaveRowUnavailableTip(deck: DeckState, stem: StemControl): string | null {
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

export function stemWaveRowColor(stem: StemControl): string {
	return STEM_COLORS[stem];
}
