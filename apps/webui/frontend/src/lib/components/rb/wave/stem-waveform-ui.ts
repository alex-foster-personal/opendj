/** Pure helpers for stem mini-waveform rows (issue #1036). */

import type { DeckState } from '$lib/rb/deck-state-types';
import { stemColorFromToken } from '$lib/rb/stem-colors';
import { STEM_CONTROLS, STEM_LAYOUT_CONTROLS, type StemControls } from '$lib/rb/stem-graph';
import type { StemControl, StemLayout } from '$lib/rb/stem-types';
import type { PerformanceCommand } from '$lib/rb/performance-ipc-types';

export type StemRowAudibility = 'audible' | 'muted' | 'silenced_by_solo';

/** Paint alpha for a row nobody can hear; mimics the stem chips' 0.3 unavailable opacity. */
export const STEM_WAVE_ROW_SILENT_ALPHA = 0.3;

/** Layout when the deck's stem-set is ready; throws if ready with no layout. */
export function readyStemLayout(deck: DeckState): StemLayout {
	if (deck.stems.status !== 'ready' || deck.stems.layout === null) {
		throw new Error('stems ready with no layout');
	}
	return deck.stems.layout;
}

/**
 * What the listener hears from one stem row, by the engine's own gate
 * (stemPartGains in $lib/rb/stem-graph): mute wins; solo counts only over the
 * controls the layout owns; demucs4 bass/other follow the instrumental group;
 * an AUDFID-02 held stem-set (playback 'original') plays the mix, so every
 * stem is audible. Gain is ignored (gain as height is out of scope).
 */
export function stemRowAudibility(
	controls: StemControls,
	stem: StemControl,
	playback: 'original' | 'stems' | undefined,
	layout: StemLayout = 'demucs4'
): StemRowAudibility {
	if (playback === 'original') return 'audible';

	const parent: StemControl | undefined =
		layout === 'demucs4' && (stem === 'bass' || stem === 'other') ? 'instrumental' : undefined;
	const group = parent === undefined ? undefined : controls[parent];
	if (controls[stem].muted || group?.muted) return 'muted';

	const owned = STEM_LAYOUT_CONTROLS[layout];
	const anySolo = owned.some((s) => controls[s].solo);
	const soloedThrough =
		controls[stem].solo ||
		group?.solo === true ||
		(layout === 'demucs4' &&
			stem === 'instrumental' &&
			(controls.bass.solo || controls.other.solo));

	if (anySolo && !soloedThrough) return 'silenced_by_solo';
	return 'audible';
}

export function stemWaveRowAlpha(audibility: StemRowAudibility): number {
	switch (audibility) {
		case 'audible':
			return 1;
		case 'muted':
		case 'silenced_by_solo':
			return STEM_WAVE_ROW_SILENT_ALPHA;
		default: {
			const _exhaustive: never = audibility;
			throw new RangeError(`stemWaveRowAlpha: unknown audibility ${_exhaustive}`);
		}
	}
}

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

/**
 * The one stem the listener hears alone (one exclusive, unmuted solo, or one
 * part left audible by muting the rest, as ACA does), else null: several
 * audible parts, several solos, a muted solo, or a held original (STEMPERF).
 */
export function soleAudibleStem(
	controls: StemControls,
	playback: 'original' | 'stems' | undefined,
	layout: StemLayout = 'demucs4'
): StemControl | null {
	if (playback === 'original') return null;
	const owned = STEM_LAYOUT_CONTROLS[layout];
	const soloed = owned.filter((s) => controls[s].solo);
	if (soloed.length === 1) {
		const stem = soloed[0];
		return stemRowAudibility(controls, stem, playback, layout) === 'audible' ? stem : null;
	}
	if (soloed.length > 1) return null;
	// No solo: the others muted (ACA quick mode) leave one audible part.
	const audible = owned.filter(
		(s) => stemRowAudibility(controls, s, playback, layout) === 'audible'
	);
	return audible.length === 1 ? audible[0] : null;
}
