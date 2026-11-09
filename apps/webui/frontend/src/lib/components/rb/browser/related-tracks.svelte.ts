/** LIBUX-10046: the Related tab's fetch state. A monotonic token keeps a stale
 * response from overwriting a newer one; a null seed is the idle state. */

import type { RelatedCriteriaPrefs } from '$lib/rb/related-criteria-prefs';
import type { RelatedHitWire, RelatedResultsWire } from '$lib/rb/api-rb';

export type RelatedTracksState =
	| { kind: 'idle' }
	| { kind: 'loading' }
	| { kind: 'loaded'; seedTitle: string; items: RelatedHitWire[] }
	| { kind: 'error'; message: string };

type FetchRelated = (
	stableId: string,
	criteria: RelatedCriteriaPrefs
) => Promise<RelatedResultsWire>;

export function createRelatedTracks(deps: { fetch: FetchRelated }) {
	let state = $state<RelatedTracksState>({ kind: 'idle' });
	let token = 0;

	/** Resolves with the error message on failure (for a toast), else null. */
	async function refresh(
		seed: string | null,
		seedTitle: string,
		criteria: RelatedCriteriaPrefs
	): Promise<string | null> {
		const mine = ++token;
		if (seed === null) {
			state = { kind: 'idle' };
			return null;
		}
		state = { kind: 'loading' };
		try {
			const body = await deps.fetch(seed, criteria);
			if (mine === token) state = { kind: 'loaded', seedTitle, items: body.items };
			return null;
		} catch (exc) {
			const message = `related tracks failed: ${String(exc)}`;
			if (mine === token) state = { kind: 'error', message };
			return message;
		}
	}

	return {
		get state(): RelatedTracksState {
			return state;
		},
		refresh
	};
}
