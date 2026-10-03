/** Partner stable_ids for the reference master deck (PAIR-04 library chrome). */
import { listPairingsFor } from '$lib/api';
import { subscribeKind } from '$lib/api/events-bus';

export interface PairingIndexOptions {
	/** Called once when a lookup FAILS after a success (or at first failure),
	 * so the panel can say so. A failed lookup is never published as an empty
	 * partner set without this firing; repeated failures do not re-notify
	 * until a lookup succeeds again. */
	onError?: (message: string) => void;
}

export class PairingIndex {
	partnerIds = $state<ReadonlySet<string>>(new Set());
	/** Non-null while the latest lookup FAILED: the partner set is then
	 * unknown, not empty, and the panel has been told via onError. */
	error = $state<string | null>(null);
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
	private readonly _onError: ((message: string) => void) | undefined;

	constructor(options: PairingIndexOptions = {}) {
		this._onError = options.onError;
	}

	start(getMasterStableId: () => string | null): void {
		this.stop();
		this._unsubs.push(
			subscribeKind('pairings', () => void this.bump(getMasterStableId))
		);
		this._unsubs.push(
			subscribeKind('tracks', () => void this.refresh(getMasterStableId))
		);
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
			return;
		}
		this._masterStableId = sid;
		if (this._publishedFor !== sid) {
			this._publishedFor = null;
			this.partnerIds = new Set();
		}
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
		} catch (exc) {
			if (generation !== this._generation) return;
			// Unknown, not empty: clear the underlines (they would be stale or
			// wrong) and SAY the lookup failed, so it cannot read as "no pairings".
			this.partnerIds = new Set();
			this._publishedFor = null;
			const message = `pairing lookup failed: ${exc instanceof Error ? exc.message : String(exc)}`;
			const firstFailure = this.error === null;
			this.error = message;
			if (firstFailure) this._onError?.(message);
		}
	}

	/** Force reload after capture even when master stable id unchanged. */
	async bump(getMasterStableId: () => string | null): Promise<void> {
		this._masterStableId = null;
		await this.refresh(getMasterStableId);
	}
}
