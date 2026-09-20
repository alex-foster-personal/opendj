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

interface TrackRow {
	stable_id: string;
	file_exists: boolean;
}

interface Cue {
	in_ms: number;
	kind: string;
	name: string | null;
}

interface AnlzPayload {
	cues: Cue[];
}

interface CueTrack {
	stable_id: string;
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
	const listing = await request.get(`${API_BASE}/api/v1/tracks?limit=200&available=true`);
	expect(listing.ok(), 'real available-track listing must succeed').toBeTruthy();
	const rows = (await listing.json()) as { items: TrackRow[] };
	for (const row of rows.items) {
		if (!row.file_exists) continue;
		const response = await request.get(`${API_BASE}/api/v1/tracks/${row.stable_id}/anlz`);
		if (!response.ok()) continue;
		const anlz = (await response.json()) as AnlzPayload;
		if (anlz.cues.length > 0) return { stable_id: row.stable_id, cues: anlz.cues };
	}
	return null;
}

async function _redPixelCount(page: Page): Promise<number> {
	return page.locator('[data-testid="deck-waveform-canvas-1"]').evaluate((node) => {
		const canvas = node as HTMLCanvasElement;
		const ctx = canvas.getContext('2d');
		if (ctx === null) throw new Error('cue QA could not read waveform canvas pixels');
		const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
		let count = 0;
		for (let i = 0; i < pixels.length; i += 4) {
			if (pixels[i] > 150 && pixels[i] > pixels[i + 1] * 2 && pixels[i] > pixels[i + 2] * 2) count += 1;
		}
		return count;
	});
}

test('cue-laden track renders red waveform triangles and strip cue letters', async ({ page, request }, testInfo: TestInfo) => {
	test.skip(IS_GENERATED_FIXTURE, 'generated fixture has no rekordbox ANLZ cues; run in real-library mode');
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	const track = await _findCueTrack(request);
	expect(track, 'real library must expose an on-disk track with stored cues').not.toBeNull();
	if (track === null) throw new Error('cue track discovery returned null after assertion');

	await _dispatch(page, { type: 'load', deck: 1, stable_id: track.stable_id });
	await expect(page.locator('[data-testid="deck-cue-letter-1"]')).toHaveCount(
		track.cues.filter((cue) => cue.kind !== 'loop').length
	);
	await _dispatch(page, { type: 'seek', deck: 1, position_ms: track.cues[0].in_ms });
	await expect.poll(() => _redPixelCount(page), { message: 'stored cue must paint red waveform pixels' }).toBeGreaterThan(0);

	await testInfo.attach('cue-marker-qa.json', {
		body: JSON.stringify({ stable_id: track.stable_id, cues: track.cues, red_pixels: await _redPixelCount(page) }, null, 2),
		contentType: 'application/json'
	});
	await testInfo.attach('cue-marker-qa.png', { body: await page.screenshot(), contentType: 'image/png' });
});
