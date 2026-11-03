/**
 * Issue #8339: one read of `/tempo-stability` per served grid, shared by the
 * deck strip's tempo lane and the big waveform's tempo-change marks.
 *
 *   ✔︎ ✅ 🎯 One request per served grid while one is in flight or answered.
 *     [if] each repaint refetches the same grid [then ⛔️] broken
 *   ✔︎ ✅ 🎯 A failed read is stored and worded, then retried on an ask at
 *     least TEMPO_STABILITY_RETRY_MS later, never at once.
 *     [if] a failed read leaves the lane loading forever [then ⛔️] broken
 *     [if] a persistent server error refetches in a loop [then ⛔️] broken
 */

import { api } from '../api/client';
import { tempoStabilityNeedsFetch, type TempoStabilityView } from './tempo-stability';

const _entries = $state<Record<string, TempoStabilityView & { at?: number }>>({});

const IDLE: TempoStabilityView = { state: 'idle' };

/** The answer for a key from `tempoStabilityKey`; idle until requested. */
export function tempoStabilityFor(key: string): TempoStabilityView {
	return _entries[key] ?? IDLE;
}

/** Read `stableId`'s tempo stability under `key`, unless it is already read or reading. */
export function requestTempoStability(stableId: string, key: string): void {
	if (!tempoStabilityNeedsFetch(_entries[key], Date.now())) return;
	_entries[key] = { state: 'loading' };
	void api
		.GET('/api/v1/tracks/{stable_id}/tempo-stability', {
			params: { path: { stable_id: stableId } }
		})
		.then(({ data, error }) => {
			if (data === undefined) throw new Error(`tempo stability: ${JSON.stringify(error ?? 'empty response')}`);
			_entries[key] = { state: 'ready', value: data };
		})
		.catch((err: unknown) => {
			_entries[key] = { state: 'error', message: err instanceof Error ? err.message : String(err), at: Date.now() };
		});
}
