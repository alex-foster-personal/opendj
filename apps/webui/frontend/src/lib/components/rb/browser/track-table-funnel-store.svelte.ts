/**
 * Per-pane track-table column funnel state (in memory only, like pane search).
 */
import { SvelteMap } from 'svelte/reactivity';

import {
	EMPTY_TRACK_TABLE_COLUMN_FILTERS,
	type TrackTableColumnFilters
} from './track-table-column-filters';

export class TrackTableFunnelStore {
	#byPane = new SvelteMap<string, TrackTableColumnFilters>();

	get(paneKey: string): TrackTableColumnFilters {
		return this.#byPane.get(paneKey) ?? EMPTY_TRACK_TABLE_COLUMN_FILTERS;
	}

	set(paneKey: string, filters: TrackTableColumnFilters): void {
		this.#byPane.set(paneKey, filters);
	}

	patchColour(paneKey: string, colour: TrackTableColumnFilters['colour']): void {
		this.set(paneKey, { ...this.get(paneKey), colour });
	}
}

/** One store per app lifetime; panes keyed by TrackTable ``restoreKey``. */
export const trackTableFunnelStore = new TrackTableFunnelStore();
