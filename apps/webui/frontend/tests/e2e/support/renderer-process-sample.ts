import { execFileSync } from 'node:child_process';
import type { CDPSession } from '@playwright/test';

export interface ProcessInfoEntry {
	id: number;
	type: string;
}

export interface RendererProcessIds {
	rendererPid: number | null;
	gpuPid: number | null;
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

function readPsSample(pid: number): { rss_mb: number; cpu_percent: number } | null {
	try {
		const output = execFileSync('ps', ['-o', 'rss=,%cpu=', '-p', String(pid)], {
			encoding: 'utf-8'
		}).trim();
		if (output.length === 0) {
			return null;
		}
		const [rssRaw, cpuRaw] = output.split(/\s+/);
		const rssKb = Number(rssRaw);
		const cpuPercent = Number(cpuRaw);
		if (!Number.isFinite(rssKb) || !Number.isFinite(cpuPercent)) {
			return null;
		}
		return { rss_mb: rssKb / 1024, cpu_percent: cpuPercent };
	} catch {
		return null;
	}
}

export async function sampleRendererProcessFootprint(
	cdp: CDPSession
): Promise<{ footprint_mb: number | null; cpu_percent: number | null }> {
	try {
		const response = await cdp.send('SystemInfo.getProcessInfo');
		const processInfo = (response as { processInfo?: ProcessInfoEntry[] }).processInfo;
		if (!Array.isArray(processInfo)) {
			return { footprint_mb: null, cpu_percent: null };
		}
		const { rendererPid, gpuPid } = selectRendererProcessIds(processInfo);
		if (rendererPid === null) {
			return { footprint_mb: null, cpu_percent: null };
		}
		const rendererSample = readPsSample(rendererPid);
		if (rendererSample === null) {
			return { footprint_mb: null, cpu_percent: null };
		}
		let footprintMb = rendererSample.rss_mb;
		let cpuPercent = rendererSample.cpu_percent;
		if (gpuPid !== null) {
			const gpuSample = readPsSample(gpuPid);
			if (gpuSample !== null) {
				footprintMb += gpuSample.rss_mb;
				cpuPercent += gpuSample.cpu_percent;
			}
		}
		return { footprint_mb: footprintMb, cpu_percent: cpuPercent };
	} catch {
		return { footprint_mb: null, cpu_percent: null };
	}
}
