import { expect, test } from '@playwright/test';
import type { APIRequestContext, Page, TestInfo } from '@playwright/test';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { PerformanceCommand } from '../../src/lib/rb/performance-ipc.svelte';

const API_BASE = process.env.PERFORMANCE_E2E_API_BASE ?? 'http://127.0.0.1:8686';
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const DATA_DIR = process.env.MDT_DATA_DIR ?? join(REPOSITORY_ROOT, 'data');
const IS_GENERATED_FIXTURE = existsSync(join(DATA_DIR, 'fixture-revision.txt'));
const WAVE_WINDOW_S = 24;

interface TrackRow {
	stable_id: string;
	file_exists: boolean;
	duration_ms: number | null;
}

interface Cue {
	in_ms: number;
	kind: string;
	name: string | null;
	slot: string | null;
	is_loop: boolean;
	out_ms: number | null;
}

interface AnlzPayload {
	cues: Cue[];
}

interface CueTrack {
	stable_id: string;
	duration_ms: number;
	cues: Cue[];
}

async function _dispatch(page: Page, command: PerformanceCommand): Promise<void> {
	await page.evaluate(async (message) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		await ipc.dispatch(message);
	}, command);
}

async function _findCueTrack(request: APIRequestContext): Promise<CueTrack | null> {
	let cursor: string | null = null;
	do {
		const cursorParam = cursor === null ? '' : `&cursor=${encodeURIComponent(cursor)}`;
		const listing = await request.get(`${API_BASE}/api/v1/tracks?limit=1000&available=true${cursorParam}`);
		expect(listing.ok(), 'real available-track listing must succeed').toBeTruthy();
		const rows = (await listing.json()) as { items: TrackRow[]; next_cursor: string | null };
		for (const row of rows.items) {
			if (!row.file_exists) continue;
			const response = await request.get(`${API_BASE}/api/v1/tracks/${row.stable_id}/anlz`);
			if (!response.ok()) {
				throw new Error(`ANLZ lookup failed for ${row.stable_id}: HTTP ${response.status()}`);
			}
			const anlz = (await response.json()) as AnlzPayload;
			const redCue = anlz.cues.find((cue) => cue.kind === 'memory');
			const letterCue = anlz.cues.find((cue) => cue.slot !== null && !cue.is_loop);
			if (redCue !== undefined && letterCue !== undefined && row.duration_ms !== null) {
				return { stable_id: row.stable_id, duration_ms: row.duration_ms, cues: anlz.cues };
			}
		}
		cursor = rows.next_cursor;
	} while (cursor !== null);
	return null;

}

async function _redPixelCount(page: Page, expectedXRatio: number): Promise<number> {
	return page.locator('[data-wave-surface="row"][data-deck="1"] canvas').evaluate((node, ratio) => {
		const canvas = node as HTMLCanvasElement;
		const ctx = canvas.getContext('2d');
		if (ctx === null) throw new Error('cue QA could not read waveform canvas pixels');
		const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
		const expectedX = Math.round(canvas.width * ratio);
		let count = 0;
		for (let y = 0; y < Math.min(12, canvas.height); y += 1) {
			for (let x = Math.max(0, expectedX - 8); x <= Math.min(canvas.width - 1, expectedX + 8); x += 1) {
				const i = (y * canvas.width + x) * 4;
				if (pixels[i] > 150 && pixels[i] > pixels[i + 1] * 2 && pixels[i] > pixels[i + 2] * 2) count += 1;
			}
		}
		return count;
	}, expectedXRatio);
}

function _seekOffsetMs(cueMs: number, durationMs: number): number {
	const forward = cueMs + 500;
	return forward < durationMs - 100 ? forward : Math.max(0, cueMs - 500);
}

function _expectedCueXRatio(cueMs: number, seekMs: number): number {
	return 0.5 - (seekMs - cueMs) / 1000 / WAVE_WINDOW_S;
}

test('cue-laden track renders red waveform triangles and strip cue letters', async ({ page, request }, testInfo: TestInfo) => {
	if (IS_GENERATED_FIXTURE) {
		throw new Error('cue-marker-qa.real-library.spec.ts requires PERFORMANCE_E2E_FIXTURE=0 and a real library');
	}
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	const track = await _findCueTrack(request);
	expect(track, 'real library must expose an on-disk track with stored cues').not.toBeNull();
	if (track === null) throw new Error('cue track discovery returned null after assertion');

	await _dispatch(page, { type: 'load', deck: 1, stable_id: track.stable_id });
	const redCue = track.cues.find((cue) => cue.kind === 'memory');
	if (redCue === undefined) throw new Error('cue track lost its discovered memory cue');
	const letterCues = track.cues.filter((cue) => cue.slot !== null && !(cue.is_loop && cue.out_ms !== null));
	await expect(page.locator('[data-testid="deck-cue-letter-1"]')).toHaveCount(
		letterCues.length
	);
	const seekMs = _seekOffsetMs(redCue.in_ms, track.duration_ms);
	await _dispatch(page, { type: 'seek', deck: 1, position_ms: seekMs });
	const expectedXRatio = _expectedCueXRatio(redCue.in_ms, seekMs);
	await expect
		.poll(() => _redPixelCount(page, expectedXRatio), { message: 'memory cue must paint red waveform pixels near its expected position' })
		.toBeGreaterThan(0);

	await testInfo.attach('cue-marker-qa.json', {
		body: JSON.stringify({ stable_id: track.stable_id, cues: track.cues, seek_ms: seekMs, expected_x_ratio: expectedXRatio, red_pixels: await _redPixelCount(page, expectedXRatio) }, null, 2),
		contentType: 'application/json'
	});
	await testInfo.attach('cue-marker-qa.png', { body: await page.screenshot(), contentType: 'image/png' });
});
