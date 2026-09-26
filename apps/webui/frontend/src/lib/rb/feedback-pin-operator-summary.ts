import { pinStatus, summarizePinStatuses, type LifecyclePin } from './feedback';
import { pinVisualState } from './feedback-pin-partial';

/**
 * Operator-facing pin buckets for pin 6af63c5e9b7c / FB-20. the maintainer's vocabulary
 * on the comment-icon hover, not raw lifecycle field names.
 *
 * Mapping (FB-20):
 * - sent to queue: pin.status is null/undefined (untriaged)
 * - in-progress: status open, derived partial overlay, or linked progress node
 *   in building/partial (fleet work in flight)
 * - delegated: status issued (agent filed / triaged)
 * - fixed / merged / blocked / harvested: same lifecycle statuses as the board
 *
 * A pin may increment more than one bucket (e.g. issued + fleet building).
 */
export interface PinOperatorBreakdown {
	total: number;
	sent_to_queue: number;
	in_progress: number;
	delegated: number;
	fixed: number;
	merged: number;
	blocked: number;
	harvested: number;
}

export interface ProgressNodeLite {
	status?: string | null;
	links?: { issues?: ReadonlyArray<string | number> } | null;
}

const PARTIAL_NOTE_PREFIX = /^partial:/i;
const REMAINING_WORK_REF = /(#\d+|https?:\/\/\S+)/;

/** Parse #N or trailing /issues/N from progress link entries. */
export function parseProgressIssueRef(raw: string | number): number | null {
	if (typeof raw === 'number' && Number.isInteger(raw) && raw > 0) return raw;
	const text = String(raw).trim();
	const hash = text.match(/#(\d+)\b/);
	if (hash) return Number.parseInt(hash[1], 10);
	const url = text.match(/\/issues\/(\d+)(?:$|[/?#])/);
	if (url) return Number.parseInt(url[1], 10);
	const bare = text.match(/^(\d+)$/);
	if (bare) return Number.parseInt(bare[1], 10);
	return null;
}

export function parsePinIssueUrl(issueUrl: string | null | undefined): number | null {
	if (!issueUrl) return null;
	const m = issueUrl.match(/\/issues\/(\d+)(?:$|[/?#])/);
	return m ? Number.parseInt(m[1], 10) : null;
}

function _fleetProgressIssues(nodes: readonly ProgressNodeLite[] | undefined): ReadonlySet<number> {
	const out = new Set<number>();
	if (!nodes) return out;
	for (const node of nodes) {
		const st = node.status ?? '';
		if (st !== 'building' && st !== 'partial') continue;
		for (const raw of node.links?.issues ?? []) {
			const n = parseProgressIssueRef(raw);
			if (n != null) out.add(n);
		}
	}
	return out;
}

export function isPartialAgentNote(note: string | null | undefined): boolean {
	if (!note) return false;
	const trimmed = note.trim();
	return PARTIAL_NOTE_PREFIX.test(trimmed) && REMAINING_WORK_REF.test(trimmed);
}

export function summarizePinOperatorBuckets(
	pins: readonly LifecyclePin[],
	progressNodes?: readonly ProgressNodeLite[]
): PinOperatorBreakdown {
	const fleetIssues = _fleetProgressIssues(progressNodes);
	const breakdown: PinOperatorBreakdown = {
		total: 0,
		sent_to_queue: 0,
		in_progress: 0,
		delegated: 0,
		fixed: 0,
		merged: 0,
		blocked: 0,
		harvested: 0
	};

	for (const pin of pins) {
		const status = pinStatus(pin);
		if (status === 'archived') continue;
		breakdown.total += 1;

		if (pin.status === null || pin.status === undefined) {
			breakdown.sent_to_queue += 1;
		}
		if (status === 'open' || pinVisualState(pin) === 'partial') {
			breakdown.in_progress += 1;
		}
		if (status === 'issued') {
			breakdown.delegated += 1;
		}
		const issueNum = parsePinIssueUrl(pin.issue_url);
		if (issueNum != null && fleetIssues.has(issueNum)) {
			breakdown.in_progress += 1;
		}
		if (status === 'fixed') breakdown.fixed += 1;
		else if (status === 'merged') breakdown.merged += 1;
		else if (status === 'blocked') breakdown.blocked += 1;
		else if (status === 'harvested') breakdown.harvested += 1;
	}

	return breakdown;
}

/** Single-line title for the comment-pin control (pin 6af63c5e9b7c). */
export function describePinOperatorSummary(breakdown: PinOperatorBreakdown): string {
	const parts = [
		`${breakdown.sent_to_queue} sent to queue`,
		`${breakdown.in_progress} in-progress`,
		`${breakdown.delegated} delegated`,
		`${breakdown.fixed} fixed`,
		`${breakdown.merged} merged`
	];
	if (breakdown.blocked > 0) parts.push(`${breakdown.blocked} blocked`);
	if (breakdown.harvested > 0) parts.push(`${breakdown.harvested} harvested`);
	return `Active comment pins: ${breakdown.total} total - ${parts.join(', ')}`;
}

/** First ControlExplainer bullet when the daemon serves /comments/summary. */
export function describePinOperatorExplainerLine(breakdown: PinOperatorBreakdown): string {
	return describePinOperatorSummary(breakdown);
}

/** Lifecycle summary string (legacy fallback when summary endpoint is absent). */
export function describePinLifecycleSummaryFromPins(pins: readonly LifecyclePin[]): string {
	const summary = summarizePinStatuses(pins);
	return (
		`Active comment pins: ${summary.total} total - ` +
		`${summary.untriaged} untriaged, ${summary.open} open, ${summary.issued} issued, ${summary.blocked} blocked, ` +
		`${summary.fixed} fixed, ${summary.merged} merged, ${summary.harvested} harvested`
	);
}
