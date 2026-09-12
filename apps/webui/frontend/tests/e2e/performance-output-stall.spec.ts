import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

const UI_BASE = (
	process.env.PERFORMANCE_E2E_BASE_URL ??
	process.env.PERFORMANCE_E2E_UI_BASE ??
	''
).replace(/\/$/, '');

interface PerfEventRow {
	kind: string;
	severity?: string;
	message?: string;
}

async function waitForIpc(page: Page): Promise<void> {
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
}

test('frozen getOutputTimestamp stalls output health, toasts, logs, and recovers', async ({ page }) => {
	test.setTimeout(60_000);
	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto(`${UI_BASE}/performance`);
	await waitForIpc(page);

	await page.getByRole('button', { name: /all tracks/i }).click();
	const stableId = await page.locator('.track-row[data-stable-id]').first().getAttribute('data-stable-id');
	expect(stableId, 'fixture library must expose a track row').toBeTruthy();

	await page.evaluate(async (sid) => {
		const ipc = window.musicDjToolsPerformance!;
		await ipc.dispatch({ type: 'load', deck: 1, stable_id: sid! });
		await ipc.dispatch({ type: 'play', deck: 1, playing: true });
	}, stableId);

	await page.waitForFunction(() => {
		const q = window.musicDjToolsPerformance!.query();
		const output = (window as Window & { __mdtAudioOutput?: () => { verdict: string } }).__mdtAudioOutput?.();
		return q.decks[1].playing === true && output !== undefined && output.verdict !== 'idle';
	});

	await page.evaluate(() => {
		const proto = AudioContext.prototype;
		const orig = proto.getOutputTimestamp;
		let frozen: number | null = null;
		proto.getOutputTimestamp = function (this: AudioContext): AudioTimestamp {
			const ts = orig.call(this);
			if (frozen === null && ts.contextTime !== undefined) frozen = ts.contextTime;
			const contextTime = frozen ?? ts.contextTime;
			if (contextTime === undefined) {
				return { performanceTime: ts.performanceTime ?? 0 };
			}
			return { contextTime, performanceTime: ts.performanceTime ?? 0 };
		};
	});

	const clientError = page.waitForRequest(
		(req) =>
			req.url().includes('/api/v1/client-errors') &&
			req.method() === 'POST' &&
			(req.postData()?.includes('audio-output-stalled') ?? false),
		{ timeout: 8_000 }
	);
	await clientError;

	await expect
		.poll(async () =>
			page.evaluate(() => {
				const read = (window as Window & { __mdtAudioOutput?: () => { verdict: string } }).__mdtAudioOutput;
				return read?.().verdict ?? 'missing';
			})
		)
		.toBe('stalled');

	await expect(page.getByText(/output position stalled/i)).toBeVisible();

	const perfRows = await page.evaluate(() => {
		const read = (window as Window & { __mdtPerfLog?: () => readonly PerfEventRow[] }).__mdtPerfLog;
		return read?.() ?? [];
	});
	expect(perfRows.some((row) => row.kind === 'audio-output-stalled' && row.severity === 'error')).toBeTruthy();
	expect(
		perfRows.some(
			(row) => row.kind === 'audio-output-rebound' || row.kind === 'audio-output-rebind-failed'
		)
	).toBeTruthy();
	expect(
		perfRows.some(
			(row) => row.kind === 'audio-output-recreated' || row.kind === 'audio-output-recreate-failed'
		)
	).toBeTruthy();

	await page.evaluate(() => {
		delete (AudioContext.prototype as { getOutputTimestamp?: AudioContext['getOutputTimestamp'] })
			.getOutputTimestamp;
	});
});
