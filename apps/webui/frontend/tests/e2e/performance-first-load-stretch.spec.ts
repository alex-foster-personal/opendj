import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

const API_BASE = process.env.PERFORMANCE_E2E_API_BASE ?? 'http://127.0.0.1:8686';
const UI_BASE = (
	process.env.PERFORMANCE_E2E_BASE_URL ??
	process.env.PERFORMANCE_E2E_UI_BASE ??
	''
).replace(/\/$/, '');

const STRETCH_CREATE_CEILING_MS = 5_000;

async function firstPlayableStableId(request: APIRequestContext): Promise<string | null> {
	const response = await request.get(`${API_BASE}/api/v1/tracks?limit=50&available=true`);
	if (!response.ok()) return null;
	const payload = (await response.json()) as {
		items?: Array<{ stable_id?: string; file_exists?: boolean }>;
	};
	const track = (payload.items ?? []).find(
		(item) => typeof item.stable_id === 'string' && item.file_exists === true
	);
	return track?.stable_id ?? null;
}

async function waitForDeckLoad(page: Page, stableId: string): Promise<void> {
	await page.waitForFunction(
		(sid) => window.musicDjToolsPerformance!.query().decks[1].stable_id === sid,
		stableId,
		{ timeout: 10_000 }
	);
}

test('first IPC load after a fresh /performance open succeeds without a Signalsmith create timeout', async ({
	page,
	request
}) => {
	test.setTimeout(60_000);
	const stableId = await firstPlayableStableId(request);
	test.skip(
		stableId === null,
		'real library must expose at least one on-disk track for the agent IPC load path'
	);

	const consoleLines: string[] = [];
	page.on('console', (msg) => consoleLines.push(msg.text()));
	page.on('pageerror', (error) => consoleLines.push(error.message));

	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto(`${UI_BASE}/performance`);
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, {
		timeout: 30_000
	});

	await page.evaluate(async (sid) => {
		await window.musicDjToolsPerformance!.dispatch({ type: 'load', deck: 1, stable_id: sid! });
	}, stableId);
	await waitForDeckLoad(page, stableId!);

	const firstQuery = await page.evaluate(() => window.musicDjToolsPerformance!.query());
	expect(firstQuery.decks[1].title.length).toBeGreaterThan(0);
	expect(firstQuery.decks[1].processor_error).toBeNull();
	expect(firstQuery.decks[1].command_error).toBeNull();
	const firstStages = firstQuery.decks[1].last_load_stages;
	expect(Number.isFinite(firstStages?.stretchCreate)).toBe(true);
	expect(firstStages!.stretchCreate!).toBeLessThan(STRETCH_CREATE_CEILING_MS);

	await page.evaluate(async (sid) => {
		await window.musicDjToolsPerformance!.dispatch({ type: 'load', deck: 1, stable_id: sid! });
	}, stableId);
	await waitForDeckLoad(page, stableId!);

	const secondQuery = await page.evaluate(() => window.musicDjToolsPerformance!.query());
	expect(secondQuery.decks[1].title.length).toBeGreaterThan(0);
	expect(secondQuery.decks[1].processor_error).toBeNull();
	expect(secondQuery.decks[1].command_error).toBeNull();

	const bodyText = await page.locator('body').innerText();
	expect(bodyText).not.toContain('StretchCommandTimeoutError');
	expect(bodyText).not.toContain('processor creation timed out');
	expect(bodyText).not.toContain('Deck 1 load failed');

	for (const line of consoleLines) {
		expect(line).not.toContain('processor creation timed out');
	}
});
