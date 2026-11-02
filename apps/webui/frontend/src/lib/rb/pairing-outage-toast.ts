/** One error toast per pairing outage, however many pairing stores fail in it.
 *
 * The graph and the index refresh on the same change, so one outage can fail
 * both in turn. The latch re-arms only once BOTH stores have settled with no
 * error. A store that recovers while the other is still in flight does not
 * re-arm it, so that store's next failure in the same outage stays silent. */

interface PairingStoreState {
	/** Non-null while the store's latest lookup failed. */
	readonly error: string | null;
	/** True while the store's latest lookup has not settled yet. */
	readonly pending: boolean;
}

export function pairingOutageToaster(
	push: (message: string) => void
): (graph: PairingStoreState, index: PairingStoreState) => void {
	let toasted = false;
	return (graph, index) => {
		const outage = graph.error ?? index.error;
		if (outage !== null) {
			if (toasted) return;
			toasted = true;
			push(outage);
			return;
		}
		if (!graph.pending && !index.pending) toasted = false;
	};
}
