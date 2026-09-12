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

test('a hanging AudioContext resume reports audio-output-dead and keeps the page responsive', async ({
	page
}) => {
	test.setTimeout(60_000);
	await page.addInitScript(() => {
		const proto = AudioContext.prototype;
		Object.defineProperty(proto, 'state', {
			configurable: true,
			get() {
				return 'suspended';
			}
		});
		proto.resume = function () {
			return new Promise(() => {});
		};
	});

	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto(`${UI_BASE}/performance`);
	await waitForIpc(page);

	await page.getByRole('button', { name: /all tracks/i }).click();
	const firstRow = page.locator('[data-testid="track-row"]').first();
	await expect(firstRow).toBeVisible({ timeout: 20_000 });
	const stableId = await firstRow.getAttribute('data-stable-id');
	expect(stableId, 'fixture library must expose a track row').toBeTruthy();

	await Promise.race([
		page.evaluate(async (sid) => {
			const ipc = window.musicDjToolsPerformance!;
			await ipc.dispatch({ type: 'load', deck: 1, stable_id: sid! });
		}, stableId),
		new Promise<never>((_resolve, reject) => {
			setTimeout(() => reject(new Error('IPC load exceeded 20s')), 20_000);
		})
	]);

	const clientError = page.waitForRequest(
		(req) =>
			req.url().includes('/api/v1/client-errors') &&
			req.method() === 'POST' &&
			(req.postData()?.includes('audio-output-dead') ?? false),
		{ timeout: 8_000 }
	);

	await Promise.race([
		page.evaluate(async () => {
			const ipc = window.musicDjToolsPerformance!;
			try {
				await ipc.dispatch({ type: 'play', deck: 1, playing: true });
			} catch {
				// play must reject rather than hang the evaluate
			}
		}),
		new Promise<never>((_resolve, reject) => {
			setTimeout(() => reject(new Error('IPC play evaluate exceeded 8s')), 8_000);
		})
	]);

	await clientError;

	await expect(page.getByText(/watchdog window/i)).toBeVisible();

	const perfRows = await page.evaluate(() => {
		const read = (window as Window & { __mdtPerfLog?: () => readonly PerfEventRow[] }).__mdtPerfLog;
		return read?.() ?? [];
	});
	expect(perfRows.some((row) => row.kind === 'audio-output-dead' && row.severity === 'error')).toBeTruthy();

	const title = await page.evaluate(() => document.title);
	expect(typeof title).toBe('string');
	expect(title.length).toBeGreaterThan(0);

	const query = await page.evaluate(() => window.musicDjToolsPerformance!.query());
	expect(query).toBeTruthy();
});
