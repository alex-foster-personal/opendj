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
	compressed_mb?: unknown;
	members?: unknown;
}

function _kernelLevelOrNull(value: unknown): number | null {
	return value === 1 || value === 2 || value === 4 ? value : null;
}

function _finiteOrNull(value: unknown): number | null {
	return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** Probe role slugs from JSONL fallback; never shown raw in PerfMeters (DEVLOOP-11). */
const PROBE_ROLE_TO_OPENDJ_LABEL: Record<string, string> = {
	'python-engine': 'opendj-engine',
	'desktop-shell': 'opendj-desktop',
	'webkit-webcontent': 'opendj-webcontent',
	'webkit-gpu': 'opendj-gpu',
	'webkit-networking': 'opendj-networking',
	'webkit-other': 'opendj-webkit'
};

function _labelFromProbeRole(role: string): string {
	if (role.startsWith('opendj-')) return role;
	return PROBE_ROLE_TO_OPENDJ_LABEL[role] ?? 'unnamed';
}

function _membersFromRoles(byRole: unknown): ProcessFamilyMember[] {
	if (typeof byRole !== 'object' || byRole === null) return [];
	const members: ProcessFamilyMember[] = [];
	for (const [rawName, rawMb] of Object.entries(byRole as Record<string, unknown>)) {
		const mb = _finiteOrNull(rawMb);
		if (mb === null) continue;
		const label =
			typeof rawName === 'string' && rawName.length > 0
				? _labelFromProbeRole(rawName)
				: 'unnamed';
		members.push({ label, mb: Math.round(mb) });
	}
	return members;
}

function _mbFromMember(member: Record<string, unknown>): number | null {
	const rss = _finiteOrNull(member.rss_mb);
	if (rss !== null) return Math.round(rss);
	const footprint = _finiteOrNull(member.physical_footprint_mb);
	if (footprint !== null) return Math.round(footprint);
	return null;
}

function _membersFromLive(rawMembers: unknown): ProcessFamilyMember[] | null {
	if (!Array.isArray(rawMembers)) return null;
	const members: ProcessFamilyMember[] = [];
	for (const raw of rawMembers) {
		if (typeof raw !== 'object' || raw === null) continue;
		const record = raw as Record<string, unknown>;
		const name = record.name;
		const label = typeof name === 'string' && name.length > 0 ? name : 'unnamed';
		members.push({ label, mb: _mbFromMember(record) });
	}
	return members;
}

/** Turn one engine response into a snapshot, or null when unavailable. */
export function processFamilyFrom(body: ProcessesResponse): ProcessFamilySnapshot | null {
	if (body.available !== true) return null;
	const live = _membersFromLive(body.members);
	return {
		kernelLevel: _kernelLevelOrNull(body.kernel_memory_pressure_level),
		churnScore: _finiteOrNull(body.churn_score),
		compressorRate:
			_finiteOrNull(body.compressor_rate) ?? _finiteOrNull(body.compressed_mb),
		members: live !== null ? live : _membersFromRoles(body.by_role_mb)
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
