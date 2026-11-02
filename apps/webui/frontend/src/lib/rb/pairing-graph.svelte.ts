/** Every pairing in the library, as a partner map (PAIR-09 library chrome).
 *
 * PairingIndex answers "partners of the master"; this answers "is this row
 * paired at all, and is its partner on a deck", which the zigzag edge needs
 * for every visible row. Pairings are few (hand-made or captured), so one
 * `GET /api/v1/pairings` per change is cheaper than a lookup per row. */
import { listPairings } from '$lib/api';
import { subscribeKind, subscribeResync } from '$lib/api/events-bus';
import { capabilities } from '$lib/api/capabilities.svelte';
import { buildFlags } from '$lib/api/store-build.svelte';
import { buildPartnerMap } from './pairing-row';

// Re-exported so BrowserPanel reaches the toaster through the store it already imports.
export { pairingOutageToaster } from './pairing-outage-toast';

/** PAIR-09: the looping in-row pulses, off unless this flag is on. */
export const PAIR_PULSES_FLAG = 'library.pair_edge_pulses';

/** Only the engine daemon serves /api/v1/flags; the legacy one would 404 and log it. */
function loadFlagsOnEngine(): void {
	void capabilities.probe().then((flavor) => {
		if (flavor === 'engine') void buildFlags.load();
	});
}

/** Window in which a pairing change announced twice reloads once. */
const REFRESH_COALESCE_MS = 150;

export class PairingGraph {
	partners = $state<ReadonlyMap<string, ReadonlySet<string>>>(new Map());
	/** Non-null while the latest load FAILED: edges are unknown, not absent. */
	error = $state<string | null>(null);
	/** True while the latest load is in flight, so a failure-then-recovery
	 * outage latch can wait for the other store before it re-arms. */
	pending = $state(false);
	private _unsubs: Array<() => void> = [];
	private _generation = 0;
	private _coalesceTimer: ReturnType<typeof setTimeout> | null = null;

	/** Whether paired rows with a loaded partner loop the earlier pulses. */
	get pulses(): boolean {
		return buildFlags.flag(PAIR_PULSES_FLAG)?.enabled === true;
	}

	start(): void {
		this.stop();
		loadFlagsOnEngine();
		this._unsubs.push(subscribeKind('pairings', () => this.requestRefresh()));
		// A pairing made while the socket was down, or in a sequence gap,
		// arrives as a resync rather than a replayed kind event.
		this._unsubs.push(subscribeResync(() => void this.refresh()));
		void this.refresh();
	}

	stop(): void {
		for (const u of this._unsubs) u();
		this._unsubs = [];
		this._cancelCoalesced();
		this._generation += 1;
		this.partners = new Map();
		this.error = null;
		this.pending = false;
	}

	/** A pairing write reaches this graph twice: the tab's own announcement and the
	 * engine's pairings event, a few milliseconds apart. Trailing debounce, so one
	 * change costs one full-list GET. */
	requestRefresh(): void {
		// Invalidate any load already in flight now, not when the debounce fires:
		// its answer predates this change and must not publish.
		this._generation += 1;
		this._cancelCoalesced();
		this.pending = true;
		this._coalesceTimer = setTimeout(() => {
			this._coalesceTimer = null;
			void this.refresh();
		}, REFRESH_COALESCE_MS);
	}

	private _cancelCoalesced(): void {
		if (this._coalesceTimer === null) return;
		clearTimeout(this._coalesceTimer);
		this._coalesceTimer = null;
	}

	async refresh(): Promise<void> {
		this._generation += 1;
		const generation = this._generation;
		this.pending = true;
		try {
			const pairings = await listPairings();
			if (generation !== this._generation) return;
			this.partners = buildPartnerMap(pairings);
			this.error = null;
			this.pending = false;
		} catch (exc) {
			if (generation !== this._generation) return;
			this.pending = false;
			this.partners = new Map();
			this.error = `pairing list failed: ${exc instanceof Error ? exc.message : String(exc)}`;
		}
	}
}
