import { hasRealBeatGrid } from '$lib/player/grid-features';
import type { AnlzData } from '$lib/rb/anlz-types';

/**
 * The deck strip's provider chip (DECKUX-LABEL-01): which producer the deck's
 * beatgrid (and, when it is knowable from this payload, its waveform) came
 * from, as one small neutral line such as "Grid: own · Wave: own".
 *
 * It replaced a centred green "native grid" overlay that read as a status
 * warning on every deck with an unmapped track. Every state here is an
 * expected one, so none of them is styled as an error.
 */
export interface GridProviderChip {
	text: string;
	title: string;
}

const SOURCE_LABEL = { own: 'own', rekordbox: 'rekordbox' } as const;

/** The strip chip for one deck's analysis payload; null while no payload is loaded. */
export function gridProviderChip(anlz: AnlzData | null): GridProviderChip | null {
	if (anlz === null) return null;
	const grid = anlz.beatgrid;
	const gridLabel = SOURCE_LABEL[grid.source];
	// A track rekordbox never mapped has no rekordbox waveform either, so its
	// waveform is ours. For a mapped track this payload does not say which
	// waveform lane served it, so the chip names the grid only.
	const unmapped = anlz.beatgrid_source_basis === 'unmapped-default';
	const wave = unmapped ? ' · Wave: own' : '';
	if (grid.source === 'own' && anlz.beatgrid_own_unavailable_reason !== null) {
		return {
			text: `Grid: own pending${wave}`,
			title: `No Open DJ beatgrid for this track yet: ${anlz.beatgrid_own_unavailable_reason}`
		};
	}
	if (grid.status === 'missing' || grid.status === 'failed' || !hasRealBeatGrid(grid.beats)) {
		const why = grid.reason ?? `${gridLabel} has no beatgrid for this track`;
		return {
			text: `Grid: none${wave}`,
			title: `${why}. Quantize and Beat Sync stay off on this deck.`
		};
	}
	let title: string;
	if (grid.source === 'rekordbox') {
		title = 'Beatgrid from rekordbox (PQTZ).';
	} else if (unmapped) {
		title = 'Beatgrid and waveform measured by Open DJ: rekordbox has no analysis for this track.';
	} else {
		title = 'Beatgrid measured by Open DJ (own is selected for the beatgrid lane).';
	}
	return { text: `Grid: ${gridLabel}${wave}`, title };
}
