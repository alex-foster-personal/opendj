import { execFileSync } from 'node:child_process';
import type { APIRequestContext, CDPSession } from '@playwright/test';

export interface ProcessInfoEntry {
	id: number;
	type: string;
}

export interface ProcessFamilyMember {
	rss_mb?: unknown;
	source?: unknown;
}

export interface ProcessTelemetryBody {
	available?: unknown;
	members?: unknown;
}

/** Every Chromium process `SystemInfo.getProcessInfo` lists: browser,
 * renderers, gpu-process and utility helpers. The page's own renderer cannot
 * be told apart from spare or extension renderers by type or order, so the
 * whole family is the measured unit, the same scope PERFMODE-15 samples. */
export function selectChromiumFamilyPids(processInfo: readonly ProcessInfoEntry[]): number[] {
	if (!processInfo.some((entry) => entry.type === 'renderer')) {
		throw new Error('CDP SystemInfo.getProcessInfo reported no renderer process');
	}
	return processInfo.map((entry) => {
		if (!Number.isInteger(entry.id) || entry.id <= 0) {
			throw new Error(`CDP SystemInfo.getProcessInfo entry has no usable pid: ${JSON.stringify(entry)}`);
		}
		return entry.id;
	});
}

/** The engine family members walked live this request (`source: 'live'`).
 *
 * The endpoint also merges `source: 'probe_log'` members: processes from the
 * native probe's last JSONL record that are not live now. That record can be
 * days old (71 h on the reference Mac, Fri 25 Sep 2026) and describes the
 * packaged app, not this browser session, so counting it adds the same stale
 * constant to Gig and Library and drags the ratio toward 1. A member with any
 * other source means an engine older than the provenance field: fail loudly
 * rather than guess from field shape. */
function liveFamilyMembers(body: ProcessTelemetryBody): ProcessFamilyMember[] {
	if (body.available !== true || !Array.isArray(body.members)) {
		throw new Error(
			'process telemetry reported available!==true; cannot measure the engine process family'
		);
	}
	const live: ProcessFamilyMember[] = [];
	for (const raw of body.members) {
		if (typeof raw !== 'object' || raw === null) {
			throw new Error(`process telemetry member is not an object: ${JSON.stringify(raw)}`);
		}
		const member = raw as ProcessFamilyMember;
		if (member.source === 'live') {
			live.push(member);
		} else if (member.source === 'probe_log') {
			continue;
		} else {
			throw new Error(
				`process telemetry member has no known source (engine predates member provenance?): ${JSON.stringify(raw)}`
			);
		}
	}
	return live;
}

/** Live engine family size (the python engine plus every descendant, e.g.
 * stem workers). A leaked worker is one extra live member; an exited process
 * still listed from the probe log is not counted at all. */
export function countLiveFamilyMembers(body: ProcessTelemetryBody): number {
	return liveFamilyMembers(body).length;
}

/** Sum of the live engine family's resident footprint (`rss_mb`, one unit for
 * every counted member) via the same `/api/v1/performance/telemetry/processes`
 * `members` list -- the half of the KPI's process family that never runs
 * inside the browser CDP can see, so a renderer-only sample undercounts it. */
export function sumEngineFamilyFootprintMb(body: ProcessTelemetryBody): number {
	const live = liveFamilyMembers(body);
	if (live.length === 0) {
		throw new Error('process telemetry listed no live member; the engine family was not measured');
	}
	let total = 0;
	for (const member of live) {
		const mb = member.rss_mb;
		if (typeof mb !== 'number' || !Number.isFinite(mb)) {
			throw new Error(
				`process telemetry live member has no finite rss_mb: ${JSON.stringify(member)}`
			);
		}
		total += mb;
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
 * - every Chromium process CDP's browser target lists (stands in for the
 *   packaged WKWebView, which Playwright's Chrome is not -- see PR #3679
 *   review),
 * - PLUS the python engine and every process it spawns (stem workers),
 *   read from the telemetry endpoint's `source: 'live'` members only.
 *
 * `cdp` must be a BROWSER-target session (`browser.newBrowserCDPSession()`):
 * a page session rejects SystemInfo.getProcessInfo on every call.
 *
 * CPU is the Chromium family only: the telemetry endpoint's `members` carry
 * footprint (`rss_mb`) but no per-process CPU or PID, so the engine family's
 * CPU share cannot be attributed from any existing endpoint without a backend
 * change (tracked, not fabricated here).
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
	let footprintMb = 0;
	let cpuPercent = 0;
	for (const pid of selectChromiumFamilyPids(processInfo)) {
		const processSample = readPsSample(pid);
		footprintMb += processSample.rss_mb;
		cpuPercent += processSample.cpu_percent;
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
