/**
 * Stems separation, from the UI's side: the plan, the enqueue, and the
 * arithmetic behind the TopBar progress bar.
 *
 * Everything testable lives here rather than in a `.svelte` file. The unit
 * suite is `node --test` with no DOM, so a component cannot be mounted and
 * anything worth asserting has to be reachable by import. The two components
 * that use this module (`StemsProgress.svelte`, `StemsPrompt.svelte`) are
 * therefore thin: they render what these functions return.
 *
 * NO FABRICATED PROGRESS. `stemsProgress` reduces real engine job rows and
 * nothing else. The asymptotic row-wash in `job-progress.svelte.ts` is a
 * different thing for a different surface (per-track analysis with no server
 * feedback); an engine-fed bar that invented motion would be lying about
 * money being spent.
 */

import { api, unwrap } from '$lib/api/client';

import { type Job, isTerminal, jobsStore } from './jobs-store.svelte';

/** The engine kind. Must match `apps/stems/job.py::JOB_KIND`. */
export const STEMS_JOB_KIND = 'stems.separate';

/** Modal rungs, in ladder order. Mirrors `apps/stems/tiers.py::TIER_ORDER`
 * filtered to `where == 'modal'`; the plan endpoint refuses anything else. */
export const STEMS_TIERS = ['S', 'M', 'L'] as const;
export type StemsTier = (typeof STEMS_TIERS)[number];
export const DEFAULT_STEMS_TIER: StemsTier = 'M';

/** What separating the library would take. Mirrors `StemsPlanOut`. */
export interface StemsPlan {
	tier: string;
	tier_name: string;
	/** library rows carrying a file path: pending + ready + unavailable */
	total: number;
	pending: number;
	ready: number;
	unavailable: number;
	estimate_seconds: number;
	estimate_usd: number;
	/** How this build reaches a GPU: 'relay' or 'direct'. */
	transport: string;
	/** Why a run cannot start on this build, or null when it can. The counts
	 * above describe work that exists; this says whether the machine can do it.
	 * Without it the prompt offers a button, POST /api/v1/jobs accepts the job,
	 * and the worker dies on a missing credential after the wizard said Done. */
	transport_refusal: string | null;
}

/** Aggregate state of every stems job the store knows about. */
export interface StemsProgress {
	/** Is any stems job still running or queued? Drives whether the bar shows. */
	active: boolean;
	/** 0..1 across all active jobs, weighted equally per job. */
	progress: number;
	running: number;
	queued: number;
	failed: number;
	succeeded: number;
	/** The newest active job's message, or null. */
	message: string | null;
}

const ACTIVE_STATUSES = new Set(['queued', 'running', 'cancelling']);

/** Every stems job in the store, newest first (the store's own order). */
export function stemsJobs(jobs: readonly Job[]): Job[] {
	return jobs.filter((job) => job.kind === STEMS_JOB_KIND);
}

/**
 * Reduce stems job rows to one bar.
 *
 * Averaged per JOB, not per track. The job row carries a single 0..1 and no
 * per-track denominator, so weighting by track count would need a number the
 * row does not have -- and inventing one is how a bar ends up disagreeing
 * with the drawer beside it. Two jobs at 50% read as 50%.
 *
 * A queued job counts as 0 rather than being skipped: work that has not
 * started is still work outstanding, and skipping it makes the bar jump
 * backwards the moment it starts.
 */
export function stemsProgress(jobs: readonly Job[]): StemsProgress {
	const mine = stemsJobs(jobs);
	const active = mine.filter((job) => ACTIVE_STATUSES.has(job.status));
	const total = active.reduce((sum, job) => sum + clamp01(job.progress), 0);
	return {
		active: active.length > 0,
		progress: active.length === 0 ? 0 : total / active.length,
		running: mine.filter((job) => job.status === 'running').length,
		queued: mine.filter((job) => job.status === 'queued').length,
		failed: mine.filter((job) => job.status === 'failed').length,
		succeeded: mine.filter((job) => job.status === 'succeeded').length,
		message: active.find((job) => job.message)?.message ?? null
	};
}

function clamp01(value: number): number {
	if (!Number.isFinite(value)) return 0;
	return Math.max(0, Math.min(1, value));
}

/**
 * The bar's hover title. House rule: a numeric readout always says what the
 * number is, so this names the jobs behind the percentage rather than
 * leaving a bare bar on screen with no way to ask what it means.
 */
export function stemsProgressTitle(state: StemsProgress): string {
	if (!state.active) {
		if (state.failed > 0) {
			return `Stems separation: no job running. ${state.failed} failed, ${state.succeeded} succeeded. Open JOBS for the error.`;
		}
		return 'Stems separation: no job running.';
	}
	const parts = [
		`Stems separation ${Math.round(state.progress * 100)}% across ${state.running + state.queued} job(s)`,
		`${state.running} running, ${state.queued} queued`
	];
	if (state.failed > 0) parts.push(`${state.failed} failed`);
	if (state.message) parts.push(state.message);
	return `${parts.join('. ')}. Separation runs on Modal GPUs and costs real money; see JOBS for per-job detail.`;
}

/** Human summary of a plan, for the install prompt. Every number named. */
export function stemsPlanSummary(plan: StemsPlan): string {
	const minutes = Math.max(1, Math.round(plan.estimate_seconds / 60));
	const cost = plan.estimate_usd < 0.01 ? 'under $0.01' : `about $${plan.estimate_usd.toFixed(2)}`;
	return `${plan.pending} of ${plan.total} tracks still need stems (${plan.ready} already done, ${plan.unavailable} have no audio on this machine). Roughly ${minutes} min on ${plan.tier_name} GPUs, ${cost}.`;
}

// ----- HTTP ---------------------------------------------------------------

/** What a stems run would take, before committing to it. */
export async function fetchStemsPlan(tier: StemsTier = DEFAULT_STEMS_TIER): Promise<StemsPlan> {
	return (await unwrap(
		api.GET('/api/v1/stems/plan', { params: { query: { tier } } })
	)) as StemsPlan;
}

/**
 * Enqueue separation for every track that still needs it.
 *
 * Sends a SCOPE, not a list of ids. The set is resolved in the worker at run
 * time, so a scan that finishes between this call and the spawn is included
 * rather than silently missed -- which is the normal case during install,
 * where the first scan and this prompt overlap.
 */
export async function enqueueStemsForPending(
	tier: StemsTier = DEFAULT_STEMS_TIER
): Promise<Job> {
	const job = (await unwrap(
		api.POST('/api/v1/jobs', {
			body: { kind: STEMS_JOB_KIND, payload: { scope: 'pending', tier } }
		})
	)) as Job;
	jobsStore.upsert(job);
	return job;
}

/** Enqueue separation for named tracks. The right-click / per-track path. */
export async function enqueueStemsForTracks(
	stableIds: readonly string[],
	tier: StemsTier = DEFAULT_STEMS_TIER
): Promise<Job> {
	if (stableIds.length === 0) {
		throw new Error('enqueueStemsForTracks needs at least one stable id');
	}
	const job = (await unwrap(
		api.POST('/api/v1/jobs', {
			body: { kind: STEMS_JOB_KIND, payload: { stable_ids: [...stableIds], tier } }
		})
	)) as Job;
	jobsStore.upsert(job);
	return job;
}

export { isTerminal };
