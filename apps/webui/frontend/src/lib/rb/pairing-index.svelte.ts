/** Partner stable_ids for the reference master deck (PAIR-04 library chrome). */
import { listPairingsFor } from '$lib/api';
import { subscribeKind } from '$lib/api/events-bus';

export class PairingIndex {
	partnerIds = $state<ReadonlySet<string>>(new Set());
	private _masterStableId: string | null = null;
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
		void this.refresh(getMasterStableId);
	}

	stop(): void {
		for (const u of this._unsubs) u();
		this._unsubs = [];
		this._generation += 1;
		this.partnerIds = new Set();
		this._masterStableId = null;
	}

	async refresh(getMasterStableId: () => string | null): Promise<void> {
		this._generation += 1;
		const generation = this._generation;
		const sid = getMasterStableId();
		if (sid === null) {
			this._masterStableId = null;
			this.partnerIds = new Set();
			return;
		}
		this._masterStableId = sid;
		try {
			const pairings = await listPairingsFor(sid);
			if (generation !== this._generation) return;
			const ids = new Set<string>();
			for (const p of pairings) {
				if (p.from_stable_id === sid) ids.add(p.to_stable_id);
				if (p.to_stable_id === sid) ids.add(p.from_stable_id);
			}
			this.partnerIds = ids;
		} catch {
			if (generation !== this._generation) return;
			this.partnerIds = new Set();
		}
	}

	/** Force reload after capture even when master stable id unchanged. */
	async bump(getMasterStableId: () => string | null): Promise<void> {
		this._masterStableId = null;
		await this.refresh(getMasterStableId);
	}
}
