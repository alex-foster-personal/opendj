/**
 * Pin f85c5881: lazy per-track read of the beatgrid's source and confidence,
 * for the library BPM hover. Nothing is fetched until a BPM cell is hovered;
 * an answer is reused for GRID_PROVENANCE_TTL_MS.
 *
 *   ✔︎ ✅ 🎯 One request per track while one is in flight or fresh.
 *     [if] sweeping the pointer down the column refetches a fresh row [then ⛔️] broken
 *   ✔︎ ✅ 🎯 A failed read is stored and worded in the hover, then retried on
 *     the next hover.
 *     [if] a failed read leaves the hover saying "loading" forever [then ⛔️] broken
 */

import { api } from '../api/client';
import {
	gridProvenanceNeedsFetch,
	type GridProvenanceEntry,
	type GridProvenanceView
} from './grid-provenance';

const _entries = $state<Record<string, GridProvenanceEntry>>({});

const IDLE: GridProvenanceView = { state: 'idle' };

export function gridProvenanceFor(stableId: string): GridProvenanceView {
	return _entries[stableId] ?? IDLE;
}

export function requestGridProvenance(stableId: string): void {
	const now = Date.now();
	if (!gridProvenanceNeedsFetch(_entries[stableId], now)) return;
	_entries[stableId] = { state: 'loading', at: now };
	void api
		.GET('/api/v1/tracks/{stable_id}/grid-provenance', {
			params: { path: { stable_id: stableId } }
		})
		.then(({ data }) => {
			if (data === undefined) throw new Error('grid provenance: empty response');
			_entries[stableId] = { state: 'ready', value: data, at: Date.now() };
		})
		.catch((err: unknown) => {
			_entries[stableId] = {
				state: 'error',
				message: err instanceof Error ? err.message : String(err),
				at: Date.now()
			};
		});
}
