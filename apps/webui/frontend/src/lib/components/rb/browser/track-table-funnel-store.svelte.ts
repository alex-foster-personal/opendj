/**
 * Per-pane track-table column funnel state (in memory only, like pane search).
 */
import { SvelteMap } from 'svelte/reactivity';

import {
	EMPTY_TRACK_TABLE_COLUMN_FILTERS,
	type TrackTableColumnFilters
} from './track-table-column-filters';
import { effectiveColumnFilters, type TrackFilterState } from './track-filter-state';

export class TrackTableFunnelStore {
	#byPane = new SvelteMap<string, TrackTableColumnFilters>();
	#suspended = new SvelteMap<string, boolean>();
	/** Issue #10043: the mounted table's pane, the one the `library_filter` command reads and sets. */
	activePane = $state<string | null>(null);

	get(paneKey: string): TrackTableColumnFilters {
		return this.#byPane.get(paneKey) ?? EMPTY_TRACK_TABLE_COLUMN_FILTERS;
	}

	set(paneKey: string, filters: TrackTableColumnFilters): void {
		this.#byPane.set(paneKey, filters);
	}

	/** Issue #10043: one pane's criteria plus its suspend switch, as the Track Filter bar shows them. */
	readState(paneKey: string): TrackFilterState {
		return { ...this.get(paneKey), suspended: this.#suspended.get(paneKey) ?? false };
	}

	/** Issue #10043: replace one pane's criteria and suspend switch at once. */
	replaceState(paneKey: string, state: TrackFilterState): void {
		const { suspended, ...filters } = state;
		this.set(paneKey, filters);
		this.#suspended.set(paneKey, suspended);
	}

	/** Issue #10043: the criteria the table applies; none while the pane's bar is switched off. */
	effective(paneKey: string): TrackTableColumnFilters {
		return effectiveColumnFilters(this.readState(paneKey));
	}

	/** Issue #10043: switch one pane's filters off (rows return) or back on, unchanged. */
	setSuspended(paneKey: string, suspended: boolean): void {
		this.#suspended.set(paneKey, suspended);
	}

	/** Issue #10043: Clear all, every criterion of one pane emptied and the switch back on. */
	clear(paneKey: string): void {
		this.replaceState(paneKey, { ...EMPTY_TRACK_TABLE_COLUMN_FILTERS, suspended: false });
	}

	patchColour(paneKey: string, colour: TrackTableColumnFilters['colour']): void {
		this.set(paneKey, { ...this.get(paneKey), colour });
	}

	/** Issue #9738: set or clear one pane's BPM range; other panes and funnels keep theirs. */
	patchBpm(paneKey: string, bpm: TrackTableColumnFilters['bpm']): void {
		this.set(paneKey, { ...this.get(paneKey), bpm });
	}

	/** Issue #9739: set or clear one pane's Key picks; other panes and funnels keep theirs. */
	patchKey(paneKey: string, key: TrackTableColumnFilters['key']): void {
		this.set(paneKey, { ...this.get(paneKey), key });
	}

	/** Issue #9740: set or clear one pane's Genre picks; other panes and funnels keep theirs. */
	patchGenre(paneKey: string, genre: TrackTableColumnFilters['genre']): void {
		this.set(paneKey, { ...this.get(paneKey), genre });
	}

	/** Issue #9740: set or clear one pane's Rating minimum; other panes and funnels keep theirs. */
	patchRating(paneKey: string, rating: TrackTableColumnFilters['rating']): void {
		this.set(paneKey, { ...this.get(paneKey), rating });
	}

	/** Issue #9742: set or clear one pane's My Tag picks and mode; other panes and funnels keep theirs. */
	patchMytag(paneKey: string, mytag: TrackTableColumnFilters['mytag']): void {
		this.set(paneKey, { ...this.get(paneKey), mytag });
	}
}

/** One store per app lifetime; panes keyed by TrackTable ``restoreKey``. */
export const trackTableFunnelStore = new TrackTableFunnelStore();
