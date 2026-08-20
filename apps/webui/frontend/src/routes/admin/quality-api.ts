/**
 * Route-local fetch helper for GET /api/v1/admin/quality-ratchet.
 *
 * The browser never reads ops/quality/baseline.json off disk - the daemon
 * serves it, so an agent can curl exactly what the panel renders. Same
 * fail-fast contract as kpi-api.ts: the response is structurally validated
 * before render, because a wrong TYPE anywhere must be an explicit error
 * rather than a junk row.
 */

import { ApiError, api, unwrap } from '$lib/api/client';

export interface QualityRatchet {
	/** ISO 8601 UTC timestamp of the baseline this ratchet was last recorded at. */
	generated: string;
	/** Metric key (e.g. "ruff.total") -> current allowance. Lower is better for every key here. */
	metrics: Record<string, number>;
}

function asObject(value: unknown, ctx: string): Record<string, unknown> {
	if (typeof value !== 'object' || value === null || Array.isArray(value)) {
		throw new Error(`quality ratchet: ${ctx} is not an object`);
	}
	return value as Record<string, unknown>;
}

function asString(value: unknown, ctx: string): string {
	if (typeof value !== 'string') throw new Error(`quality ratchet: ${ctx} is not a string`);
	return value;
}

function parseMetrics(raw: unknown): Record<string, number> {
	const obj = asObject(raw, 'metrics');
	const parsed: Record<string, number> = {};
	for (const [key, value] of Object.entries(obj)) {
		if (typeof value !== 'number') {
			throw new Error(`quality ratchet: metrics.${key} is not a number`);
		}
		parsed[key] = value;
	}
	return parsed;
}

export function parseQualityRatchet(raw: unknown): QualityRatchet {
	const obj = asObject(raw, 'response');
	return {
		generated: asString(obj.generated, 'generated'),
		metrics: parseMetrics(obj.metrics)
	};
}

// Exposed for the unit suite: it can hand the parser a hand-built payload
// without a network round trip through openapi-fetch's mock plumbing.
export const _parseQualityRatchetForTests = parseQualityRatchet;

//----- fetch ----------------------------------------------------------------

export async function fetchQualityRatchet(): Promise<QualityRatchet> {
	try {
		const data = await unwrap(api.GET('/api/v1/admin/quality-ratchet', { cache: 'no-store' }));
		return parseQualityRatchet(data);
	} catch (error) {
		if (error instanceof ApiError) {
			const bodyText = await error.response.text();
			throw new Error(
				`GET /api/v1/admin/quality-ratchet failed (HTTP ${error.status}): ${bodyText}`
			);
		}
		throw error;
	}
}
