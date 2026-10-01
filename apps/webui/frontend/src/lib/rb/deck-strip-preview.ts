/**
 * Resolve deck overview strip waveform before full deck ANLZ lands (LIBUX-20).
 */
import type { PreviewStripData } from '$lib/rb/api-rb';
import type { AnlzData } from '$lib/rb/anlz-types';
import type { StripWaveformBands } from '$lib/components/rb/deck/strip-waveform-render';

export type DeckStripPreviewSource = 'full' | 'cache' | 'listing' | 'none';

export interface DeckStripPreviewResolution {
	waveform: StripWaveformBands | null;
	source: DeckStripPreviewSource;
	loading: boolean;
}

/** Maps listing hydrate preview bytes into strip painter bands (LIBUX-20 / DECKUX-20). */
export function previewStripDataToStripBands(preview: PreviewStripData): StripWaveformBands {
	const n = preview.cols;
	const low: number[] = [];
	const mid: number[] = [];
	const high: number[] = [];
	const norm = preview.max > 0 ? preview.max : 1;
	for (let i = 0; i < n; i++) {
		const base = i * 3;
		low.push(preview.bands[base] / norm);
		mid.push(preview.bands[base + 1] / norm);
		high.push(preview.bands[base + 2] / norm);
	}
	return { kind: 'tri', preview: { length: n, low, mid, high } };
}

function _anlzPreview(anlz: AnlzData): StripWaveformBands | null {
	const wf = anlz.waveform;
	if (wf === undefined || wf.preview === undefined) return null;
	return { kind: wf.kind === 'mono' ? 'mono' : 'tri', preview: wf.preview };
}

export function resolveDeckStripPreview(input: {
	stableId: string | null;
	deckAnlz: AnlzData | null;
	cachedAnlz: AnlzData | null | undefined;
	listingPreview: PreviewStripData | null | undefined;
	deckPending: boolean;
	cacheLoading: boolean;
}): DeckStripPreviewResolution {
	if (input.stableId === null) {
		return { waveform: null, source: 'none', loading: false };
	}
	if (input.deckAnlz !== null) {
		const fromDeck = _anlzPreview(input.deckAnlz);
		if (fromDeck !== null) return { waveform: fromDeck, source: 'full', loading: false };
	}
	if (input.cachedAnlz !== undefined && input.cachedAnlz !== null) {
		const fromCache = _anlzPreview(input.cachedAnlz);
		if (fromCache !== null) return { waveform: fromCache, source: 'cache', loading: false };
	}
	if (input.listingPreview !== undefined && input.listingPreview !== null) {
		return {
			waveform: previewStripDataToStripBands(input.listingPreview),
			source: 'listing',
			loading: false
		};
	}
	const loading = input.deckPending || input.cacheLoading;
	return { waveform: null, source: 'none', loading };
}
