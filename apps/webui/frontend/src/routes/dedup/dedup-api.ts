/**
 * Route-local fetch helpers for GET/POST /api/v1/dedup/clusters.
 *
 * Deliberately NOT in $lib/api.ts (owned by a concurrent workflow) and does
 * NOT import from $lib/rb/* (owned by the /performance workflow) - the
 * artwork URL builder below is a local duplicate of that one-liner.
 */

import type { ClustersResponse, Decision, DecisionAction, DecisionRecord } from './types';

const BASE = typeof window === 'undefined' ? 'http://127.0.0.1:8585' : '';

function asObject(v: unknown, ctx: string): Record<string, unknown> {
	if (typeof v !== 'object' || v === null || Array.isArray(v)) {
		throw new Error(`dedup: ${ctx} is not an object`);
	}
	return v as Record<string, unknown>;
}

function asString(v: unknown, ctx: string): string {
	if (typeof v !== 'string') throw new Error(`dedup: ${ctx} is not a string`);
	return v;
}

function asStringOrNull(v: unknown, ctx: string): string | null {
	if (v === undefined) throw new Error(`dedup: ${ctx} is missing (expected string or null)`);
	return v === null ? null : asString(v, ctx);
}

function asNumberOrNull(v: unknown, ctx: string): number | null {
	if (v === undefined) throw new Error(`dedup: ${ctx} is missing (expected number or null)`);
	if (v === null) return null;
	if (typeof v !== 'number') throw new Error(`dedup: ${ctx} is not a number`);
	return v;
}

function asBoolean(v: unknown, ctx: string): boolean {
	if (typeof v !== 'boolean') throw new Error(`dedup: ${ctx} is not a boolean`);
	return v;
}

function asArray(v: unknown, ctx: string): unknown[] {
	if (!Array.isArray(v)) throw new Error(`dedup: ${ctx} is not an array`);
	return v;
}

function parseDecision(v: unknown, ctx: string): Decision | null {
	if (v === undefined) throw new Error(`dedup: ${ctx} is missing (expected object or null)`);
	if (v === null) return null;
	const o = asObject(v, ctx);
	return {
		survivor: asString(o.survivor, `${ctx}.survivor`),
		action: asString(o.action, `${ctx}.action`) as DecisionAction,
		decided_at: asString(o.decided_at, `${ctx}.decided_at`)
	};
}

function parseMember(v: unknown, ctx: string) {
	const o = asObject(v, ctx);
	return {
		stable_id: asString(o.stable_id, `${ctx}.stable_id`),
		path: asString(o.path, `${ctx}.path`),
		is_canonical: asBoolean(o.is_canonical, `${ctx}.is_canonical`),
		similarity: asNumberOrNull(o.similarity, `${ctx}.similarity`),
		title: asStringOrNull(o.title, `${ctx}.title`),
		artist: asStringOrNull(o.artist, `${ctx}.artist`),
		bpm: asNumberOrNull(o.bpm, `${ctx}.bpm`),
		key: asStringOrNull(o.key, `${ctx}.key`),
		duration_ms: asNumberOrNull(o.duration_ms, `${ctx}.duration_ms`),
		rating: asNumberOrNull(o.rating, `${ctx}.rating`),
		file_exists: asBoolean(o.file_exists, `${ctx}.file_exists`)
	};
}

function parseCluster(v: unknown, ctx: string) {
	const o = asObject(v, ctx);
	const cluster_id = o.cluster_id;
	if (typeof cluster_id !== 'number') throw new Error(`dedup: ${ctx}.cluster_id is not a number`);
	const clusterCtx = `cluster ${cluster_id}`;
	return {
		cluster_id,
		survivor_stable_id: asString(o.survivor_stable_id, `${clusterCtx}.survivor_stable_id`),
		rationale: asStringOrNull(o.rationale, `${clusterCtx}.rationale`),
		flagged_manual_review: asBoolean(o.flagged_manual_review, `${clusterCtx}.flagged_manual_review`),
		members: asArray(o.members, `${clusterCtx}.members`).map((m, i) =>
			parseMember(m, `${clusterCtx}.members[${i}]`)
		),
		decision: parseDecision(o.decision, `${clusterCtx}.decision`)
	};
}

export function validateClustersResponse(raw: unknown): ClustersResponse {
	const o = asObject(raw, 'response');
	return {
		clusters: asArray(o.clusters, 'clusters').map((c, i) => parseCluster(c, `clusters[${i}]`)),
		note: asStringOrNull(o.note, 'note')
	};
}

export async function fetchDedupClusters(): Promise<ClustersResponse> {
	let r: Response;
	try {
		r = await fetch(`${BASE}/api/v1/dedup/clusters`, { headers: { Accept: 'application/json' } });
	} catch (e) {
		throw new Error(`daemon unreachable at :8585 (${e instanceof Error ? e.message : String(e)})`);
	}
	if (!r.ok) {
		const body = await r.text();
		throw new Error(`GET /api/v1/dedup/clusters failed: ${r.status} ${body.slice(0, 300)}`);
	}
	return validateClustersResponse(await r.json());
}

export async function postDedupDecision(
	clusterId: number,
	survivor: string,
	action: DecisionAction
): Promise<DecisionRecord> {
	let r: Response;
	try {
		r = await fetch(`${BASE}/api/v1/dedup/clusters/${clusterId}/decision`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
			body: JSON.stringify({ survivor, action })
		});
	} catch (e) {
		throw new Error(`daemon unreachable at :8585 (${e instanceof Error ? e.message : String(e)})`);
	}
	if (!r.ok) {
		const body = await r.text();
		throw new Error(
			`POST /api/v1/dedup/clusters/${clusterId}/decision failed: ${r.status} ${body.slice(0, 300)}`
		);
	}
	return (await r.json()) as DecisionRecord;
}

/** URL for GET /tracks/{sid}/artwork - use directly as <img src>. Local
 * duplicate of $lib/rb/api-rb.ts's artworkUrl (that module is off-limits). */
export function dedupArtworkUrl(stableId: string): string {
	return `${BASE}/api/v1/tracks/${encodeURIComponent(stableId)}/artwork?size=s`;
}
