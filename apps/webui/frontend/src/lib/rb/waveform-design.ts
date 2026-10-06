/** Deck and library waveform paint style (DECKUX-20, issue #3991). */
/** 'blocks': single-color mirrored bars, 2px wide with a 1px gap. */
export type WaveformDesign = 'tri-band' | 'mono' | 'line' | 'blocks';

/** Stored preference: a concrete design, or 'auto' to follow the active
 * skin's declared design (ui-skin.ts SKIN_WAVE_LOOK). */
export type WaveformDesignPref = WaveformDesign | 'auto';

/** Render fallback when a frame carries no design (never a stored pref). */
export const WAVEFORM_DESIGN_DEFAULT: WaveformDesign = 'tri-band';

/** First-run stored pref: follow the skin. */
export const WAVEFORM_DESIGN_PREF_DEFAULT: WaveformDesignPref = 'auto';

const _VALID = new Set<string>(['tri-band', 'mono', 'line', 'blocks']);

export function parseWaveformDesign(raw: unknown): WaveformDesign | undefined {
	if (raw === undefined) return undefined;
	if (typeof raw !== 'string' || !_VALID.has(raw)) {
		throw new Error(`waveform_design must be tri-band|mono|line|blocks, got ${String(raw)}`);
	}
	return raw as WaveformDesign;
}

/** Undefined passes through (pref absent); 'auto' or a design passes; anything else throws. */
export function parseWaveformDesignPref(raw: unknown): WaveformDesignPref | undefined {
	if (raw === 'auto') return 'auto';
	if (raw === undefined) return undefined;
	if (typeof raw !== 'string' || !_VALID.has(raw)) {
		throw new Error(`waveform_design must be auto|tri-band|mono|line|blocks, got ${String(raw)}`);
	}
	return raw as WaveformDesign;
}
