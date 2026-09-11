/**
 * Thin reader for GET /api/v1/performance/telemetry/processes (PERFMODE-05).
 *
 * Maps the privacy-reduced probe snapshot for the open PerfMeters monitor.
 * Poll only while the monitor is open - not on every compact-bar session.
 */

import { API_BASE } from '$lib/api/base';
import type { ProcessFamilyMember, ProcessFamilySnapshot } from './perf-meter-model';

const PROCESSES_PATH = '/api/v1/performance/telemetry/processes';

interface ProcessesResponse {
	available?: unknown;
	kernel_memory_pressure_level?: unknown;
	by_role_mb?: unknown;
	churn_score?: unknown;
	compressor_rate?: unknown;
}

function _intOrNull(value: unknown): number | null {
	return typeof value === 'number' && Number.isInteger(value) ? value : null;
}

function _finiteOrNull(value: unknown): number | null {
	return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function _membersFromRoles(byRole: unknown): ProcessFamilyMember[] {
	if (typeof byRole !== 'object' || byRole === null) return [];
	const members: ProcessFamilyMember[] = [];
	for (const [rawName, rawMb] of Object.entries(byRole as Record<string, unknown>)) {
		const mb = _finiteOrNull(rawMb);
		if (mb === null) continue;
		const label = typeof rawName === 'string' && rawName.length > 0 ? rawName : 'unnamed';
		members.push({ label, mb: Math.round(mb) });
	}
	return members;
}

/** Turn one engine response into a snapshot, or null when unavailable. */
export function processFamilyFrom(body: ProcessesResponse): ProcessFamilySnapshot | null {
	if (body.available !== true) return null;
	return {
		kernelLevel: _intOrNull(body.kernel_memory_pressure_level),
		churnScore: _finiteOrNull(body.churn_score),
		compressorRate: _finiteOrNull(body.compressor_rate),
		members: _membersFromRoles(body.by_role_mb)
	};
}

let _cached: ProcessFamilySnapshot | null = null;
let _cachedAtMs = 0;

/** Last fetched snapshot, or null if never polled or unavailable. */
export function readProcessFamilySnapshot(): ProcessFamilySnapshot | null {
	return _cached;
}

/**
 * Fetch the process-family snapshot. Never throws.
 * Returns null when the engine has no sample.
 */
export async function fetchProcessFamilySnapshot(): Promise<ProcessFamilySnapshot | null> {
	try {
		const response = await fetch(`${API_BASE}${PROCESSES_PATH}`, {
			headers: { accept: 'application/json' }
		});
		if (!response.ok) return _cached;
		const body = (await response.json()) as ProcessesResponse;
		const snapshot = processFamilyFrom(body);
		if (snapshot !== null) {
			_cached = snapshot;
			_cachedAtMs = Date.now();
		}
		return snapshot;
	} catch {
		return _cached;
	}
}

/** Age of the last successful fetch in ms, or null if never fetched. */
export function processFamilySnapshotAgeMs(nowMs: number): number | null {
	if (_cachedAtMs === 0) return null;
	return Math.max(0, nowMs - _cachedAtMs);
}
