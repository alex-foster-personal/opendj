/**
 * A loaded deck showing NO STEMS looks for its stem bundle again when a stems
 * job (`library_jobs`) or a library change (`tracks`) names its track or names
 * none, and on events-bus resync, so stems written after the load are picked
 * up with no track reload (issue #9654, STEM-9934). Before each lookup it
 * clears a load-time cached "no stems" capability so the probe asks the server.
 *
 * Pure and port-injected: no engine, bus or dispatcher imports, so the unit
 * test drives it with a fake bus and fake deck state.
 */
import type { DeckId } from '$lib/rb/deck-slots';
import type { StemDeckState } from '$lib/rb/stem-types';
import { forgetStemsAbsence } from '$lib/rb/optional-resource-availability';
import { stemStatusView } from '$lib/rb/stem-status';

/** What one deck's refresh needs from the page. */
export interface LoadedDeckStemsPorts {
	deck: DeckId;
	/** The deck's live state, read at event time (never cached). */
	state: () => { stable_id: string | null; stems: StemDeckState };
	subscribeKind: (kind: 'library_jobs' | 'tracks', listener: (ids: string[]) => void) => () => void;
	subscribeResync: (listener: () => void) => () => void;
	/** False when `stem_load` cannot run here (Rust engine mode). */
	canLoadStems: () => boolean;
	/** Send the deck's existing bundle lookup: `stem_load` naming the track. */
	loadStems: (stable_id: string) => Promise<unknown>;
}

function _eventNamesDeck(ids: string[], stable_id: string): boolean {
	return ids.length === 0 || ids.includes(stable_id);
}

/** Subscribe one deck; returns unsubscribe for the component's cleanup. */
export function subscribeLoadedDeckStemsRefresh(ports: LoadedDeckStemsPorts): () => void {
	let lookupInFlight = false;

	/** `ids === null` is a resync, which always matches. */
	function _refresh(ids: string[] | null): void {
		const { stable_id, stems } = ports.state();
		if (stable_id === null) return;
		if (stemStatusView(stems).name !== 'none') return;
		if (ids !== null && !_eventNamesDeck(ids, stable_id)) return;
		if (!ports.canLoadStems()) return;
		if (lookupInFlight) return;
		lookupInFlight = true;
		// PRRT_kwDOSEvNd861ELTs: load-time TrackOut cached stems:false would make
		// probeStemArtifact answer without a GET until the cache is cleared.
		forgetStemsAbsence(stable_id);
		void ports.loadStems(stable_id).finally(() => {
			lookupInFlight = false;
		});
	}

	const unsubscribeJobs = ports.subscribeKind('library_jobs', (ids) => _refresh(ids));
	const unsubscribeTracks = ports.subscribeKind('tracks', (ids) => _refresh(ids));
	const unsubscribeResync = ports.subscribeResync(() => _refresh(null));
	return () => {
		unsubscribeJobs();
		unsubscribeTracks();
		unsubscribeResync();
	};
}
