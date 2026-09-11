/**
 * Live user-lane job state for the library (PERFBATCH-05).
 * Hydrates GET /library-jobs; refetches on library_jobs invalidation.
 * Upserts real progress into job-progress (no asymptotic fake bar).
 */
import { subscribeKind, subscribeResync } from '$lib/api/events-bus';
import { jobProgress, type JobPhase } from '$lib/rb/job-progress.svelte';
import {
	listLibraryJobs,
	type LibraryJobItem,
	type LibraryJobLane
} from '$lib/rb/api-library-jobs';

function phaseOf(state: LibraryJobItem['state']): JobPhase | null {
	if (state === 'pending') return 'queued';
	if (state === 'running') return 'running';
	if (state === 'done' || state === 'skipped') return 'done';
	if (state === 'failed' || state === 'cancelled') return 'error';
	return null;
}

function upsertReal(item: LibraryJobItem): void {
	const phase = phaseOf(item.state);
	if (phase === null) return;
	const kind = item.lane === 'stems' ? 'stems' : 'lyrics';
	jobProgress.upsert({
		stable_id: item.stable_id,
		kind,
		phase,
		progress: phase === 'done' ? 1 : phase === 'running' ? 0.15 : 0.02,
		label: item.detail ?? item.state,
		real: true
	});
}

let stems: LibraryJobItem[] = $state([]);
let lyrics: LibraryJobItem[] = $state([]);
let attached = 0;
let unsubKind: (() => void) | null = null;
let unsubResync: (() => void) | null = null;

async function hydrate(lane: LibraryJobLane): Promise<void> {
	const listing = await listLibraryJobs(lane, true);
	if (lane === 'stems') stems = listing.items;
	else lyrics = listing.items;
	for (const item of listing.items) upsertReal(item);
}

async function hydrateAll(): Promise<void> {
	await Promise.all([hydrate('stems'), hydrate('lyrics')]);
}

export const libraryJobsStore = {
	get stems() {
		return stems;
	},
	get lyrics() {
		return lyrics;
	},
	items(lane: LibraryJobLane): LibraryJobItem[] {
		return lane === 'stems' ? stems : lyrics;
	},
	async refresh(): Promise<void> {
		await hydrateAll();
	},
	attach(): void {
		attached += 1;
		if (attached !== 1) return;
		void hydrateAll();
		unsubKind = subscribeKind('library_jobs', () => {
			void hydrateAll();
		});
		unsubResync = subscribeResync(() => {
			void hydrateAll();
		});
	},
	detach(): void {
		attached = Math.max(0, attached - 1);
		if (attached !== 0) return;
		unsubKind?.();
		unsubResync?.();
		unsubKind = null;
		unsubResync = null;
	}
};
