/** Deck and library waveform paint style (DECKUX-20, issue #3991). */
/** 'blocks': single-color mirrored bars, 2px wide with a 1px gap. */
export type WaveformDesign = 'tri-band' | 'mono' | 'line' | 'blocks';

export const WAVEFORM_DESIGN_DEFAULT: WaveformDesign = 'tri-band';

const _VALID = new Set<string>(['tri-band', 'mono', 'line', 'blocks']);

export function parseWaveformDesign(raw: unknown): WaveformDesign | undefined {
	if (raw === undefined) return undefined;
	if (typeof raw !== 'string' || !_VALID.has(raw)) {
		throw new Error(`waveform_design must be tri-band|mono|line|blocks, got ${String(raw)}`);
	}
	return raw as WaveformDesign;
}
