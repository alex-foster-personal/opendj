/**
 * Reactive per-track auto-cues cache (build unit: HotCueBank).
 * Mirrors wave/beatgrid-fallback-cache.svelte.ts. Callers fetch on
 * deck.stable_id becoming non-null; a 404 ANALYSIS_NOT_FOUND is an
 * empty-slots state, never a UI error.
 *
 * .svelte.ts extension is REQUIRED for the $state rune (RECON-FRONTEND 10.1).
 */
import { fetchAutoCues, RbApiError, type AutoCuesOut } from '$lib/rb/auto-cues-api';
import { optionalResources } from '$lib/rb/optional-resource-availability';

export type AutoCuesEntry =
	| { status: 'loading' }
	| { status: 'ready'; data: AutoCuesOut }
	| { status: 'error'; code: string };

const _cache = $state<Record<string, AutoCuesEntry>>({});

/** Kick off the /auto-cues fetch for a track unless already cached/in-flight.
 * MUTATES rune state - call from $effect, never from $derived. */
export function ensureAutoCues(stable_id: string): void {
	if (_cache[stable_id] !== undefined) return;
	if (optionalResources(stable_id).autoCues === false) {
		_cache[stable_id] = { status: 'error', code: 'ANALYSIS_NOT_FOUND' };
		return;
	}
	_cache[stable_id] = { status: 'loading' };
	void fetchAutoCues(stable_id).then(
		(data: AutoCuesOut) => {
			if ((data as { proposal: unknown }).proposal !== true) {
				_cache[stable_id] = { status: 'error', code: 'NOT_A_PROPOSAL' };
				return;
			}
			_cache[stable_id] = { status: 'ready', data };
		},
		(err: unknown) => {
			if (err instanceof RbApiError) {
				_cache[stable_id] = { status: 'error', code: err.code };
				return;
			}
			_cache[stable_id] = { status: 'error', code: 'FETCH_FAILED' };
			throw err; // loud: network/shape failures must not vanish
		}
	);
}

/** Pure read; undefined = never requested for this stable_id. */
export function getAutoCuesEntry(stable_id: string): AutoCuesEntry | undefined {
	return _cache[stable_id];
}
