/**
 * Re-analyze context-menu toast copy and batch progress polling (FB-18a, #3981).
 */

import {
	backfillProgress,
	type BackfillEnqueueResult,
	type BackfillProgress
} from '$lib/rb/api-analysis-backfill';

export const REANALYZE_LANE = 'beatgrid';
export const REANALYZE_BACKEND = 'own_beatgrid.backfill';
export const REANALYZE_TOAST_GROUP = 'reanalyze-batch';

export const REANALYZE_LANE_LABEL = 'beatgrid';

export function describeReanalyzePlan(lane: string, backend: string): string {
	if (lane === REANALYZE_LANE && backend === REANALYZE_BACKEND) {
		return `${REANALYZE_LANE_LABEL} (own beatgrid backfill)`;
	}
	return `${lane} (${backend})`;
}

function topRefusalReasons(progress: BackfillProgress, cap = 3): string[] {
	const seen = new Set<string>();
	const out: string[] = [];
	for (const item of progress.items) {
		if (item.state !== 'refused') continue;
		const reason = item.reason?.trim();
		if (!reason || seen.has(reason)) continue;
		seen.add(reason);
		out.push(reason);
		if (out.length >= cap) break;
	}
	return out;
}

export function formatReanalyzeEnqueueToast(
	result: BackfillEnqueueResult,
	progress: BackfillProgress | null,
	planLabel: string
): { message: string; kind: 'info' | 'warn' | 'error'; title?: string } {
	const title = `Analysis: ${planLabel}`;
	const batch = result.batch_id;
	if (result.admitted === 0) {
		const reasons = progress ? topRefusalReasons(progress) : [];
		const reasonBit =
			reasons.length > 0 ?
				` Reasons: ${reasons.join('; ')}.`
			:	' None were queued; check that the analysis queue runner is draining.';
		return {
			title,
			message: `Re-analyze (${planLabel}): queued 0 of ${result.offered} (batch ${batch}).${reasonBit}`,
			kind: result.refused > 0 ? 'warn' : 'error'
		};
	}
	const refusedBit =
		result.refused > 0 ? ` (${result.refused} refused at enqueue).` : '';
	return {
		title,
		message: `Re-analyze (${planLabel}): queued ${result.admitted} of ${result.offered} (batch ${batch})${refusedBit} Waiting for the queue…`,
		kind: result.refused > 0 ? 'warn' : 'info'
	};
}

export function formatReanalyzeProgressToast(
	progress: BackfillProgress,
	planLabel: string
): { message: string; kind: 'info' | 'warn' | 'error'; title?: string } {
	const title = `Analysis: ${planLabel}`;
	const { counts } = progress;
	const batch = progress.batch_id;
	if (progress.state === 'queued' || progress.state === 'running') {
		const active = counts.running + counts.pending;
		return {
			title,
			message: `Re-analyze (${planLabel}): ${progress.state} — ${counts.done} done, ${active} active, ${counts.failed} failed (batch ${batch})`,
			kind: 'info'
		};
	}
	const breakdown = `done ${counts.done}, failed ${counts.failed}, skipped ${counts.skipped}, refused ${counts.refused}, cancelled ${counts.cancelled}`;
	let kind: 'info' | 'warn' | 'error' = 'info';
	if (counts.done === 0 && counts.failed > 0) kind = 'error';
	else if (counts.done === 0) kind = 'warn';
	return {
		title,
		message: `Re-analyze (${planLabel}): complete — ${breakdown} (batch ${batch})`,
		kind
	};
}

export type ReanalyzeToastSlice = {
	message: string;
	kind: 'info' | 'warn' | 'error';
	title?: string;
};

function _stripBatchSuffix(message: string): string {
	return message.replace(/\s*\(batch [^)]+\)/gu, '').replace(/\s{2,}/g, ' ').trim();
}

/** Collapsed-tray headline + expandable detail for UX-TOAST-02 (#3981 pin 0a3514651f17). */
export function toReanalyzeToastPresentation(presentation: ReanalyzeToastSlice): {
	headline: string;
	detail: string;
	kind: 'info' | 'warn' | 'error';
} {
	const detail = presentation.message;
	let headline = _stripBatchSuffix(detail);
	if (/queued 0 of/i.test(headline)) {
		headline = headline.replace(
			/^Re-analyze \((.*?)\): queued 0 of/i,
			'Re-analyze: nothing queued ($1) — queued 0 of'
		);
	}
	return { headline, detail, kind: presentation.kind };
}

export type ReanalyzeWatchCallbacks = {
	onUpdate: (progress: BackfillProgress, presentation: ReturnType<typeof formatReanalyzeProgressToast>) => void;
	onTerminal: (progress: BackfillProgress, presentation: ReturnType<typeof formatReanalyzeProgressToast>) => void;
	onError: (message: string) => void;
};

const POLL_MS = 2000;

export function watchReanalyzeBatch(
	batchId: string,
	planLabel: string,
	callbacks: ReanalyzeWatchCallbacks
): () => void {
	let stopped = false;
	let timer: ReturnType<typeof setInterval> | null = null;

	const tick = async (): Promise<void> => {
		if (stopped) return;
		try {
			const progress = await backfillProgress(batchId);
			const presentation = formatReanalyzeProgressToast(progress, planLabel);
			if (progress.state === 'done' || progress.state === 'cancelled') {
				stop();
				callbacks.onTerminal(progress, presentation);
				return;
			}
			callbacks.onUpdate(progress, presentation);
		} catch (error) {
			stop();
			callbacks.onError(error instanceof Error ? error.message : String(error));
		}
	};

	const stop = (): void => {
		stopped = true;
		if (timer !== null) {
			clearInterval(timer);
			timer = null;
		}
	};

	void tick();
	timer = setInterval(() => void tick(), POLL_MS);
	return stop;
}
