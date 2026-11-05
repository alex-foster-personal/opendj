/**
 * Route-local client for the /admin "Title fixes" tab (TFIX-04).
 *
 * Every call is the typed openapi client over /api/v1/title-fix/*, the same
 * endpoints `python -m apps.webui.title_fix_cli` drives, imported through the
 * `$lib/api` facade as lyrics-api.ts does. Errors throw ApiError
 * carrying the backend's {"detail": {code, message}} envelope; the panel shows
 * the message verbatim and never substitutes a fallback.
 */

import { api, unwrap } from '$lib/api/client';
import type { components, paths } from '$lib/api-types';

export type TitleFixConfig = components['schemas']['TitleFixConfigOut'];
export type TitleFixConfigIn = components['schemas']['TitleFixConfigIn'];
export type TitleFixScan = components['schemas']['ScanOut'];
export type TitleFixProposal = components['schemas']['ProposalOut'];
export type TitleFixPage = components['schemas']['ProposalsPage'];
export type TitleFixOutcome = components['schemas']['OutcomeOut'];
export type TitleFixBatch = components['schemas']['BatchOut'];
type ProposalsQueryWire = NonNullable<
	paths['/api/v1/title-fix/proposals']['get']['parameters']['query']
>;

export const TAG_FAMILIES = ['version', 'context', 'cover', 'project'] as const;
export const POLICIES = [
	{ value: 'off', label: 'Off (only apply what I select)' },
	{ value: 'safe_tier', label: 'Safe tier only' },
	{ value: 'gate_approved', label: 'Gate-approved (status fixed)' }
] as const;

/**
 * The warning beside Scan when the SAVED policy lets a finished scan change tracks on its own.
 * Null for 'off', where a scan only proposes.
 */
export function scanAutoApplyWarning(policy: string): string | null {
	if (policy === 'off') return null;
	const which = policy === 'safe_tier' ? 'safe-tier' : 'gate-approved';
	return `Auto-apply is on (${which}): when this scan finishes it will change tracks without a selection. Every change can still be undone below.`;
}

export interface ProposalQuery {
	state: string | null;
	fixer_status: string | null;
	source: string | null;
	tag: string | null;
	limit: number;
	offset: number;
}

export async function fetchConfig(): Promise<TitleFixConfig> {
	return unwrap(api.GET('/api/v1/title-fix/config', { cache: 'no-store' }));
}

export async function saveConfig(body: TitleFixConfigIn): Promise<TitleFixConfig> {
	return unwrap(api.PUT('/api/v1/title-fix/config', { body }));
}

export async function saveKey(apiKey: string): Promise<TitleFixConfig['key']> {
	return unwrap(api.PUT('/api/v1/title-fix/key', { body: { api_key: apiKey } }));
}

export async function clearKey(): Promise<TitleFixConfig['key']> {
	return unwrap(api.DELETE('/api/v1/title-fix/key', {}));
}

export async function startScan(limit: number | null): Promise<TitleFixScan> {
	const body = limit === null ? {} : { limit };
	return unwrap(api.POST('/api/v1/title-fix/scans', { body }));
}

export async function fetchScan(scanId: string): Promise<TitleFixScan> {
	return unwrap(
		api.GET('/api/v1/title-fix/scans/{scan_id}', {
			params: { path: { scan_id: scanId } },
			cache: 'no-store'
		})
	);
}

export async function fetchLatestScan(): Promise<TitleFixScan | null> {
	const scans = await unwrap(
		api.GET('/api/v1/title-fix/scans', { params: { query: { limit: 1 } }, cache: 'no-store' })
	);
	return scans[0] ?? null;
}

/** Drop empty filters so the query string matches what the CLI would send. */
export function proposalQueryParams(q: ProposalQuery): Record<string, string | number> {
	const out: Record<string, string | number> = { limit: q.limit, offset: q.offset };
	for (const key of ['state', 'fixer_status', 'source', 'tag'] as const) {
		const value = q[key];
		if (value !== null && value.trim() !== '') out[key] = value.trim();
	}
	return out;
}

export async function fetchProposals(q: ProposalQuery): Promise<TitleFixPage> {
	return unwrap(
		api.GET('/api/v1/title-fix/proposals', {
			// Filter values are free text in the UI; the server validates the enums and 422s a bad one.
			params: { query: proposalQueryParams(q) as ProposalsQueryWire },
			cache: 'no-store'
		})
	);
}

export async function applyProposals(ids: string[]): Promise<TitleFixOutcome> {
	return unwrap(api.POST('/api/v1/title-fix/apply', { body: { proposal_ids: ids } }));
}

export async function rejectProposals(ids: string[]): Promise<TitleFixOutcome> {
	return unwrap(api.POST('/api/v1/title-fix/reject', { body: { proposal_ids: ids } }));
}

export async function fetchBatches(): Promise<TitleFixBatch[]> {
	return unwrap(api.GET('/api/v1/title-fix/batches', { cache: 'no-store' }));
}

export async function undoBatch(batchId: string, skipConflicts: boolean): Promise<TitleFixOutcome> {
	return unwrap(
		api.POST('/api/v1/title-fix/batches/{batch_id}/undo', {
			params: { path: { batch_id: batchId } },
			body: { skip_conflicts: skipConflicts }
		})
	);
}

// ----- pure display helpers (unit-tested) ------------------------------------

export function formatUsd(value: number): string {
	return `$${value.toFixed(4)}`;
}

export function scanProgressLabel(scan: TitleFixScan): string {
	const pct = scan.requested === 0 ? 100 : Math.floor((scan.processed / scan.requested) * 100);
	return `${scan.state} - ${scan.processed}/${scan.requested} (${pct}%)`;
}

/** Spend as a fraction of the cap, clamped to [0, 1] for the meter. */
export function spendFraction(scan: TitleFixScan): number {
	if (scan.spend_cap_usd <= 0) return 1;
	return Math.min(1, Math.max(0, scan.cost_usd / scan.spend_cap_usd));
}

export function formatConfidence(value: number | null): string {
	return value === null ? '-' : value.toFixed(2);
}

/** "raw -> proposed" for one field, or null when the field is unchanged. */
export function fieldDiff(original: string, proposed: string): string | null {
	return original === proposed
		? null
		: `${original === '' ? '(blank)' : original} -> ${proposed === '' ? '(blank)' : proposed}`;
}

/** Selection can only hold pending rows; anything else is not actionable. */
export function selectableIds(items: TitleFixProposal[], selected: ReadonlySet<string>): string[] {
	return items
		.filter((p) => p.state === 'pending' && selected.has(p.proposal_id))
		.map((p) => p.proposal_id);
}
