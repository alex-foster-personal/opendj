/**
 * PERFMODE-14: assert Library mode did not leave backend stem workers running.
 */

const STEM_BACKEND_WORKER_MARKERS = [
	'stems_local_worker',
	'stem_bundle_worker',
	'bounded_file_open_worker'
] as const;

export interface ProcessTelemetryBody {
	available?: unknown;
	members?: unknown;
}

function _memberHaystack(member: Record<string, unknown>): string {
	return JSON.stringify(member);
}

/** Return stem-worker markers found in any telemetry member field. */
export function findStemBackendWorkers(body: ProcessTelemetryBody): string[] {
	if (body.available !== true || !Array.isArray(body.members)) return [];
	const hits = new Set<string>();
	for (const raw of body.members) {
		if (typeof raw !== 'object' || raw === null) continue;
		const haystack = _memberHaystack(raw as Record<string, unknown>);
		for (const marker of STEM_BACKEND_WORKER_MARKERS) {
			if (haystack.includes(marker)) hits.add(marker);
		}
	}
	return [...hits];
}

export function assertNoStemBackendWorkers(body: ProcessTelemetryBody): void {
	if (body.available !== true) {
		throw new Error(
			'process telemetry reported available!==true; cannot verify no stem backend workers ' +
				'are running (an unavailable probe is not evidence of a clean teardown)'
		);
	}
	const hits = findStemBackendWorkers(body);
	if (hits.length > 0) {
		throw new Error(`unexpected stem backend workers in process telemetry: ${hits.join(', ')}`);
	}
}
