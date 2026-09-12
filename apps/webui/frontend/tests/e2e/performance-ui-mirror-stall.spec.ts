import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

const UI_BASE = (
	process.env.PERFORMANCE_E2E_BASE_URL ??
	process.env.PERFORMANCE_E2E_UI_BASE ??
	''
).replace(/\/$/, '');

const RECEIVED_AT_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;

function isReceivedAtStamp(value: unknown): value is string {
	return typeof value === 'string' && RECEIVED_AT_RE.test(value);
}

interface PerfEventRow {
	kind: string;
	severity?: string;
	message?: string;
}

async function waitForIpc(page: Page): Promise<void> {
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
}

async function loadAndPlayDeck1(page: Page): Promise<void> {
	await page.getByRole('button', { name: /all tracks/i }).click();
	const trackRow = page.locator('[data-testid="track-row"][data-stable-id]').first();
	await expect(trackRow).toBeVisible({ timeout: 30_000 });
	const stableId = await trackRow.getAttribute('data-stable-id');
	expect(stableId, 'fixture library must expose a track row').toBeTruthy();
	await page.evaluate(async (sid) => {
		const ipc = window.musicDjToolsPerformance!;
		await ipc.dispatch({ type: 'load', deck: 1, stable_id: sid! });
		await ipc.dispatch({ type: 'play', deck: 1, playing: true });
	}, stableId);
	await page.waitForFunction(() => window.musicDjToolsPerformance!.query().decks[1].playing === true);
}

test('main-thread freeze logs mirror-stall and received_at moves forward', async ({ page }) => {
	test.setTimeout(90_000);
	const consoleLines: string[] = [];
	page.on('console', (msg) => {
		consoleLines.push(msg.text());
	});

	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto(`${UI_BASE}/performance`);
	await waitForIpc(page);
	await loadAndPlayDeck1(page);

	let before = '';
	await expect
		.poll(async () => {
			const receivedAt = await page.evaluate(async () => {
				const response = await fetch('/api/v1/state/ui-mirror');
				if (!response.ok) return null;
				const mirror = await response.json();
				const stamp = mirror.received_at;
				return typeof stamp === 'string' &&
					/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(stamp)
					? stamp
					: null;
			});
			if (receivedAt !== null) before = receivedAt;
			return receivedAt;
		})
		.not.toBeNull();

	await page.evaluate(() => {
		const end = performance.now() + 6000;
		while (performance.now() < end) {
			/* busy */
		}
	});

	await expect
		.poll(
			async () => {
				const hasConsoleStall = consoleLines.some((line) => line.includes('[perf-event] mirror-stall'));
				const { perfStall, receivedAt } = await page.evaluate(async () => {
					const read = (window as Window & { __mdtPerfLog?: () => readonly PerfEventRow[] }).__mdtPerfLog;
					const rows = read?.() ?? [];
					const perfStall = rows.find((row) => row.kind === 'mirror-stall');
					const response = await fetch('/api/v1/state/ui-mirror');
					const mirror = await response.json();
					return {
						perfStall,
						receivedAt: mirror.received_at as string | undefined
					};
				});
				const gapMatch = perfStall?.message?.match(/(\d+)ms/);
				const gapMs = gapMatch ? Number.parseInt(gapMatch[1], 10) : 0;
				const receivedMoved = isReceivedAtStamp(receivedAt) && receivedAt > before;
				return (
					hasConsoleStall &&
					perfStall?.severity === 'error' &&
					gapMs > 5000 &&
					receivedMoved
				);
			},
			{ timeout: 8_000 }
		)
		.toBe(true);
});
