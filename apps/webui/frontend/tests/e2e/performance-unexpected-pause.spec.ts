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

async function loadAndPlayDeck1(page: Page): Promise<void> {
	await page.getByRole('button', { name: /all tracks/i }).click();
	const stableId = await page.locator('.track-row[data-stable-id]').first().getAttribute('data-stable-id');
	expect(stableId, 'fixture library must expose a track row').toBeTruthy();
	await page.evaluate(async (sid) => {
		const ipc = window.musicDjToolsPerformance!;
		await ipc.dispatch({ type: 'load', deck: 1, stable_id: sid! });
		await ipc.dispatch({ type: 'play', deck: 1, playing: true });
	}, stableId);
	await page.waitForFunction(() => window.musicDjToolsPerformance!.query().decks[1].playing === true);
}

test('suspended AudioContext records audio-unexpected-pause fault and client error', async ({ page }) => {
	test.setTimeout(60_000);
	await page.addInitScript(() => {
		const Orig = window.AudioContext;
		const seen: AudioContext[] = [];
		const Wrapped = function (this: AudioContext, ...args: ConstructorParameters<typeof AudioContext>) {
			const ctx = new Orig(...args);
			seen.push(ctx);
			return ctx;
		} as unknown as typeof AudioContext;
		Wrapped.prototype = Orig.prototype;
		Object.setPrototypeOf(Wrapped, Orig);
		window.AudioContext = Wrapped;
		(window as Window & { __mdtCapturedAudioContexts?: AudioContext[] }).__mdtCapturedAudioContexts =
			seen;
	});

	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto(`${UI_BASE}/performance`);
	await waitForIpc(page);
	await loadAndPlayDeck1(page);

	const clientError = page.waitForRequest(
		(req) =>
			req.method() === 'POST' &&
			req.url().includes('/api/v1/client-errors') &&
			(req.postData()?.includes('audio-unexpected-pause') ?? false),
		{ timeout: 8_000 }
	);
	await page.evaluate(async () => {
		const contexts =
			(window as Window & { __mdtCapturedAudioContexts?: AudioContext[] }).__mdtCapturedAudioContexts ??
			[];
		const ctx = contexts.find((c) => c.state === 'running') ?? contexts.at(-1);
		if (ctx === undefined) throw new Error('no captured AudioContext');
		await ctx.suspend();
	});
	await clientError;

	const perfRows = await page.evaluate(() => {
		const read = (window as Window & { __mdtPerfLog?: () => readonly PerfEventRow[] }).__mdtPerfLog;
		return read?.() ?? [];
	});
	const fault = perfRows.find((row) => row.kind === 'audio-unexpected-pause');
	expect(fault).toBeTruthy();
	expect(fault?.severity).toBe('error');
	expect(fault?.message).toMatch(/cause=context-suspended/);
	expect(fault?.message).toMatch(/position_ms=\d+/);

	await expect
		.poll(async () => {
			return page.evaluate(async () => {
				const response = await fetch('/api/v1/state/ui-mirror');
				const mirror = await response.json();
				return mirror.audio_health?.recent_faults?.some(
					(row: { kind: string }) => row.kind === 'audio-unexpected-pause'
				);
			});
		})
		.toBe(true);
});

test('paused deck under armed AutoPlay raises no-deck-playing stall after thirty seconds', async ({
	page
}) => {
	test.setTimeout(90_000);
	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto(`${UI_BASE}/performance`);
	await waitForIpc(page);
	await loadAndPlayDeck1(page);

	await page.evaluate(() => {
		return window.musicDjToolsPerformance!.dispatch({ type: 'play', deck: 1, playing: false });
	});

	await expect
		.poll(async () => {
			return page.evaluate(async () => {
				const response = await fetch('/api/v1/state/ui-mirror');
				const mirror = await response.json();
				return mirror.autoplay_stall?.reason ?? null;
			});
		})
		.toBe('no-deck-playing');
});
