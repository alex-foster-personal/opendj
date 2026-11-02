/** Partner stable_ids for the reference master deck (PAIR-04 library chrome). */
import { listPairingsFor } from '$lib/api';
import { subscribeKind, subscribeResync } from '$lib/api/events-bus';

export class PairingIndex {
	partnerIds = $state<ReadonlySet<string>>(new Set());
	/** Non-null while the latest lookup FAILED: the partner set is then
	 * unknown, not empty. pairingOutageToaster turns it into the one toast. */
	error = $state<string | null>(null);
	/** True while the latest lookup is in flight, so a failure-then-recovery
	 * outage latch can wait for the other store before it re-arms. */
	pending = $state(false);
	private _masterStableId: string | null = null;
	/** The master whose partners `partnerIds` currently shows, so a lookup
	 * for a different master clears them instead of leaving the previous
	 * master's underlines up until the new answer lands. */
	private _publishedFor: string | null = null;
	private _unsubs: Array<() => void> = [];
	/** Bumped by every refresh and by stop(); a response publishes only while
	 * its own generation is still the latest, so a slow answer for a previous
	 * master can never overwrite the current master's partners. */
	private _generation = 0;
	start(getMasterStableId: () => string | null): void {
		this.stop();
		this._unsubs.push(
			subscribeKind('pairings', () => void this.bump(getMasterStableId))
		);
		this._unsubs.push(
			subscribeKind('tracks', () => void this.refresh(getMasterStableId))
		);
		// A pairing change missed while the socket was down arrives as a resync.
		this._unsubs.push(subscribeResync(() => void this.bump(getMasterStableId)));
		void this.refresh(getMasterStableId);
	}

	stop(): void {
		for (const u of this._unsubs) u();
		this._unsubs = [];
		this._generation += 1;
		this.partnerIds = new Set();
		this._publishedFor = null;
		this._masterStableId = null;
		this.error = null;
		this.pending = false;
	}

	async refresh(getMasterStableId: () => string | null): Promise<void> {
		this._generation += 1;
		const generation = this._generation;
		const sid = getMasterStableId();
		if (sid === null) {
			this._masterStableId = null;
			this._publishedFor = null;
			this.partnerIds = new Set();
			this.error = null;
			this.pending = false;
			return;
		}
		this._masterStableId = sid;
		if (this._publishedFor !== sid) {
			this._publishedFor = null;
			this.partnerIds = new Set();
		}
		this.pending = true;
		try {
			const pairings = await listPairingsFor(sid);
			if (generation !== this._generation) return;
			const ids = new Set<string>();
			for (const p of pairings) {
				if (p.from_stable_id === sid) ids.add(p.to_stable_id);
				if (p.to_stable_id === sid) ids.add(p.from_stable_id);
			}
			this.partnerIds = ids;
			this._publishedFor = sid;
			this.error = null;
			this.pending = false;
		} catch (exc) {
			if (generation !== this._generation) return;
			this.pending = false;
			// Unknown, not empty: clear the underlines (they would be stale or
			// wrong) and SAY the lookup failed, so it cannot read as "no pairings".
			this.partnerIds = new Set();
			this._publishedFor = null;
			this.error = `pairing lookup failed: ${exc instanceof Error ? exc.message : String(exc)}`;
		}
	}

	/** Force reload after capture even when master stable id unchanged. */
	async bump(getMasterStableId: () => string | null): Promise<void> {
		this._masterStableId = null;
		await this.refresh(getMasterStableId);
	}
}
