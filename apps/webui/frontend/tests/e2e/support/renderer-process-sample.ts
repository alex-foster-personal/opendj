import { execFileSync } from 'node:child_process';
import type { APIRequestContext, CDPSession } from '@playwright/test';

export interface ProcessInfoEntry {
	id: number;
	type: string;
}

export interface RendererProcessIds {
	rendererPid: number | null;
	gpuPid: number | null;
}

export interface ProcessFamilyMember {
	rss_mb?: unknown;
	physical_footprint_mb?: unknown;
}

export interface ProcessTelemetryBody {
	available?: unknown;
	members?: unknown;
}

export function selectRendererProcessIds(
	processInfo: readonly ProcessInfoEntry[]
): RendererProcessIds {
	let rendererPid: number | null = null;
	let gpuPid: number | null = null;
	for (const entry of processInfo) {
		if (rendererPid === null && entry.type === 'renderer') {
			rendererPid = entry.id;
		}
		if (gpuPid === null && entry.type === 'gpu-process') {
			gpuPid = entry.id;
		}
	}
	return { rendererPid, gpuPid };
}

/** Sum of the engine process family's resident footprint (the python engine
 * plus every descendant it spawns, e.g. stem workers) via the same
 * `/api/v1/performance/telemetry/processes` `members` list stem-backend-workers.ts
 * scans -- this is the half of the KPI's process family that never runs
 * inside the browser CDP can see, so a renderer-only sample undercounts it. */
export function sumEngineFamilyFootprintMb(body: ProcessTelemetryBody): number {
	if (body.available !== true || !Array.isArray(body.members)) {
		throw new Error(
			'process telemetry reported available!==true; cannot measure the engine process family'
		);
	}
	let total = 0;
	for (const raw of body.members) {
		if (typeof raw !== 'object' || raw === null) continue;
		const member = raw as ProcessFamilyMember;
		const mb =
			typeof member.rss_mb === 'number'
				? member.rss_mb
				: typeof member.physical_footprint_mb === 'number'
					? member.physical_footprint_mb
					: null;
		if (mb !== null && Number.isFinite(mb)) total += mb;
	}
	return total;
}

function readPsSample(pid: number): { rss_mb: number; cpu_percent: number } {
	const output = execFileSync('ps', ['-o', 'rss=,%cpu=', '-p', String(pid)], {
		encoding: 'utf-8'
	}).trim();
	if (output.length === 0) {
		throw new Error(`ps returned no output for pid ${pid} (process likely exited)`);
	}
	const [rssRaw, cpuRaw] = output.split(/\s+/);
	const rssKb = Number(rssRaw);
	const cpuPercent = Number(cpuRaw);
	if (!Number.isFinite(rssKb) || !Number.isFinite(cpuPercent)) {
		throw new Error(`ps returned unparseable output for pid ${pid}: ${JSON.stringify(output)}`);
	}
	return { rss_mb: rssKb / 1024, cpu_percent: cpuPercent };
}

/**
 * One footprint/CPU sample of the FULL process family under test:
 * - the Chrome renderer + gpu-process CDP drives (stands in for the packaged
 *   WKWebView, which Playwright's Chrome is not -- see PR #3679 review),
 * - PLUS the python engine and every process it spawns (stem workers),
 *   read from the same telemetry endpoint stem-backend-workers.ts uses.
 *
 * CPU is renderer+gpu only: the telemetry endpoint's `members` carry
 * footprint (rss_mb / physical_footprint_mb) but no per-process CPU or PID,
 * so the engine family's CPU share cannot be attributed from any existing
 * endpoint without a backend change (tracked, not fabricated here -- see the
 * PR's review-thread reply on this file for the explicit note).
 *
 * Every read throws rather than degrading to null: a partial-family median is
 * exactly the false-PASS class this KPI exists to catch, so a failed sample is
 * a failed sample, not a quietly smaller one. Callers apply a bounded
 * minimum-sample-count check across the dwell instead of tolerating gaps here.
 */
export async function sampleProcessFamilyFootprint(
	cdp: CDPSession,
	request: APIRequestContext,
	apiBase: string
): Promise<{ footprint_mb: number; cpu_percent: number }> {
	const response = await cdp.send('SystemInfo.getProcessInfo');
	const processInfo = (response as { processInfo?: ProcessInfoEntry[] }).processInfo;
	if (!Array.isArray(processInfo)) {
		throw new Error('CDP SystemInfo.getProcessInfo returned no processInfo array');
	}
	const { rendererPid, gpuPid } = selectRendererProcessIds(processInfo);
	if (rendererPid === null) {
		throw new Error('CDP SystemInfo.getProcessInfo reported no renderer process');
	}
	const rendererSample = readPsSample(rendererPid);
	let footprintMb = rendererSample.rss_mb;
	let cpuPercent = rendererSample.cpu_percent;
	if (gpuPid !== null) {
		// Found a gpu-process entry: its ps read must succeed too, or Gig and
		// Library medians would silently mix renderer+gpu samples with
		// renderer-only ones (the exact defect this review thread caught).
		const gpuSample = readPsSample(gpuPid);
		footprintMb += gpuSample.rss_mb;
		cpuPercent += gpuSample.cpu_percent;
	}
	const telemetryResponse = await request.get(`${apiBase}/api/v1/performance/telemetry/processes`);
	if (!telemetryResponse.ok()) {
		throw new Error(
			`process telemetry request failed: ${telemetryResponse.status()} ${telemetryResponse.statusText()}`
		);
	}
	const telemetryBody = (await telemetryResponse.json()) as ProcessTelemetryBody;
	footprintMb += sumEngineFamilyFootprintMb(telemetryBody);
	return { footprint_mb: footprintMb, cpu_percent: cpuPercent };
}
