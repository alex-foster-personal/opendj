/**
 * Edge sampler: what feeds the residency plan (#10782, spec 5.3, 6). Pure parts the store and the
 * engine share: the store's edge state read as a ResidencyView (sampler-residency.ts), and a
 * throttled runner that hands the latest view to the clip pool's `promote`.
 *
 * - An awake edge (the wave is out, or a tab is held) contributes its visible tabs (hot, in tab
 *   order) and the pages either side (warm). An asleep edge the pointer is heading for (intent)
 *   contributes its visible tabs only. An asleep edge with no intent contributes nothing.
 * - The runner runs at once when it has been idle for the throttle, else once at the end of it,
 *   always with the latest view; one run at a time. A view with nothing open and no intent cancels
 *   a pending run and arms no timer (a closed panel does no work).
 *
 * Requirements (mini-PRD):
 *   ✔︎ ✅ 🎯 the view names the awake edges' visible tabs and neighbour pages, and intent edges' tabs
 *     [if] the L edge is awake [then] its set's tabs are visible and the sets either side neighbours [else ⛔️]
 *     [if] both edges sleep with no intent [then] the view is closed with nothing in it [else ⛔️]
 *   ✔︎ ✅ 🎯 the runner is throttled, latest-wins, and idle for a closed view
 *     [if] a closed view is fed [then] no timer is armed and nothing runs [else ⛔️]
 *     [if] three views arrive inside the throttle [then] the first runs at once and only the last runs after [else ⛔️]
 */

import { pageWrap } from './sampler-sets';
import type { ResidencyView } from './sampler-residency';
import type { Edge } from './sampler-types';

/** Minimum gap between two promotions of the pool (ms), well under the wake dwell and ease-in. */
const RESIDENCY_THROTTLE_MS = 50;

/** The parts of one edge's store state the feed reads. */
interface ResidencyEdge<S> {
	set: number;
	results: readonly S[] | null;
	rpage: number;
	awake: boolean;
	intent: boolean;
}

/** The parts of the store the feed reads (structurally the `sampler` $state). */
interface ResidencyStore<S> {
	sets: readonly { items: readonly (S | null)[] }[];
	perSet: number;
	edges: Readonly<Record<Edge, ResidencyEdge<S>>>;
}

const EDGES: readonly Edge[] = ['L', 'R'];

/** The filled tabs an edge shows `delta` pages from its current set or result page (wrapping). */
function edgePageSlots<S>(store: ResidencyStore<S>, edge: Edge, delta: number): S[] {
	const e = store.edges[edge];
	let items: readonly (S | null)[];
	if (e.results !== null) {
		const pages = Math.max(1, Math.ceil(e.results.length / store.perSet));
		const p = pageWrap(e.rpage, delta, pages);
		items = e.results.slice(p * store.perSet, (p + 1) * store.perSet);
	} else if (store.sets.length > 0) {
		items = store.sets[pageWrap(e.set, delta, store.sets.length)]?.items ?? [];
	} else {
		items = [];
	}
	return items.filter((s): s is S => s !== null);
}

/** The residency view of the store: awake edges' tabs and neighbours, intent edges' tabs. */
export function residencyInputs<S>(store: ResidencyStore<S>): ResidencyView<S> {
	const visible: S[] = [];
	const neighbours: S[] = [];
	let open = false;
	let intent = false;
	for (const edge of EDGES) {
		const e = store.edges[edge];
		if (!e.awake && !e.intent) continue;
		open ||= e.awake;
		intent ||= e.intent;
		visible.push(...edgePageSlots(store, edge, 0));
		if (e.awake)
			neighbours.push(...edgePageSlots(store, edge, -1), ...edgePageSlots(store, edge, 1));
	}
	return { open, intent, visible, neighbours };
}

/** The timer and clock the runner uses (the window's in the app, fakes in tests). */
interface FeedClock {
	setTimeout(fn: () => void, ms: number): unknown;
	clearTimeout(handle: unknown): void;
	now(): number;
}

/** A throttled, latest-wins, one-at-a-time runner of `run` over residency views; idle when closed. */
export function createResidencyFeed<S>(
	run: (view: ResidencyView<S>) => Promise<void>,
	clock: FeedClock,
	throttleMs: number = RESIDENCY_THROTTLE_MS
): (view: ResidencyView<S>) => void {
	let pending: ResidencyView<S> | null = null;
	let timer: unknown = null;
	let running = false;
	let lastAt = -Infinity;

	async function runLatest(): Promise<void> {
		const view = pending;
		pending = null;
		if (view === null) return;
		running = true;
		lastAt = clock.now();
		try {
			await run(view);
		} finally {
			running = false;
			if (pending !== null) feed(pending);
		}
	}

	function feed(view: ResidencyView<S>): void {
		if (!view.open && !view.intent) {
			pending = null;
			if (timer !== null) clock.clearTimeout(timer);
			timer = null;
			return;
		}
		pending = view;
		if (timer !== null || running) return;
		const wait = lastAt + throttleMs - clock.now();
		if (wait <= 0) {
			void runLatest();
			return;
		}
		timer = clock.setTimeout(() => {
			timer = null;
			void runLatest();
		}, wait);
	}

	return feed;
}
