/** Deck and library waveform paint style (DECKUX-20, issue #3991). */
export type WaveformDesign = 'tri-band' | 'mono' | 'line';

export const WAVEFORM_DESIGN_DEFAULT: WaveformDesign = 'tri-band';

const _VALID = new Set<string>(['tri-band', 'mono', 'line']);

export function parseWaveformDesign(raw: unknown): WaveformDesign | undefined {
	if (raw === undefined) return undefined;
	if (typeof raw !== 'string' || !_VALID.has(raw)) {
		throw new Error(`waveform_design must be tri-band|mono|line, got ${String(raw)}`);
	}
	return raw as WaveformDesign;
}
