import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

/**
 * WAVEPLAN-7896..7896c: a scheduled plan draws on both deck waveforms, and moving the incoming deck moves it.
 *
 * The plan, the executor, the transport and the mirror are all real. The fixture library's generated tracks carry no
 * beatgrid, so one is added to the real /anlz answer: a dependency of the overlay, not the thing under test.
 *
 * [if] a scheduled plan shows on only one deck [then ⛔] the overlay is broken.
 * [if] the incoming deck moves and the mirror's geometry stays [then ⛔] the overlay is stale.
 * [if] the overlay outlives the plan [then ⛔] the waveform shows a plan that will not run.
 */
const BAR_MS = 2000; // the seeded 120 BPM grid
const GRID_BEATS = 28 * 4;
const PLAN_ID = 'plan-overlay-smoke';
const ORDER_TIMEOUT_MS = 20_000;

type DeckOverlay = { deck: number; role: string; start_ms: number | null; unavailable: string | null; curves: { name: string; points: number[][] }[] };
type AgentPlan = { id: string; status: string; reason: string | null; overlay: { decks: DeckOverlay[]; anchor_moved: { moved_bars: number } | null } | null };

async function _tracks(request: APIRequestContext): Promise<string[]> {
	const response = await request.get('/api/v1/tracks?limit=50&available=true');
	expect(response.ok(), 'track listing must succeed').toBeTruthy();
	const { items } = (await response.json()) as { items: { stable_id: string; file_exists: boolean }[] };
	const ids = items.filter((item) => item.file_exists).map((item) => item.stable_id);
	expect(ids.length, 'the fixture library holds two on-disk tracks').toBeGreaterThanOrEqual(2);
	return ids;
}

async function _seedGrid(page: Page): Promise<void> {
	await page.route('**/api/v1/tracks/*/anlz*', async (route) => {
		const response = await route.fetch();
		const body = (await response.json()) as { beatgrid: { beats: unknown[]; beat_count: number } };
		body.beatgrid.beat_count = GRID_BEATS;
		body.beatgrid.beats = Array.from({ length: GRID_BEATS }, (_, i) => ({ n: (i % 4) + 1, bpm: 120, t: i * 0.5 }));
		await route.fulfill({ response, json: body });
	});
}

async function _agentPlan(request: APIRequestContext): Promise<AgentPlan | null> {
	const response = await request.get('/api/v1/state/ui-mirror');
	expect(response.ok(), 'the mirror must be readable').toBeTruthy();
	return ((await response.json()) as { agent_plan: AgentPlan | null }).agent_plan;
}

/** What each deck was doing, for the failure text of an order the page never answered. */
function _decksSnapshot(page: Page): Promise<string> {
	return page.evaluate(() => {
		const { decks, master_deck } = window.musicDjToolsPerformance!.query();
		const rows = [1, 2].map((id) => ({ deck: id, ...decks[id as 1 | 2], beatgrid: decks[id as 1 | 2].beatgrid.length }));
		return JSON.stringify({ master_deck, decks: rows });
	});
}

/** The page answers a refused order inside a 200, so the step's own status is the result.
 *  The bound sits inside the test's own, so a hung order fails here with the decks in hand, not as a bare test timeout. */
async function _orderSucceeds(page: Page, request: APIRequestContext, order: object): Promise<void> {
	const response = await request.post('/api/v1/commands', { data: order, timeout: ORDER_TIMEOUT_MS }).catch(async (error: unknown) => {
		throw new Error(`the page did not answer the order: ${String(error)}\n${await _decksSnapshot(page)}`);
	});
	const text = await response.text();
	expect(response.ok(), text).toBeTruthy();
	expect((JSON.parse(text) as { steps: { status: string }[] }).steps[0]?.status, text).toBe('succeeded');
}

/** Pixels near the low EQ's amber on a deck's waveform canvas: the chip proves the state, this proves the paint.
 *  The match is loose because a 1.5 px line anti-aliases, and the fixture's waveform is gray, so nothing else matches. */
function _amberPixels(page: Page, deck: number): Promise<number> {
	return page.evaluate((id) => {
		const canvas = document.querySelector<HTMLCanvasElement>(`.rb-waverow[data-deck="${id}"] canvas.wave-seek-canvas`)!;
		const { data } = canvas.getContext('2d')!.getImageData(0, 0, canvas.width, canvas.height);
		let count = 0;
		for (let i = 0; i < data.length; i += 4) {
			if (Math.abs(data[i] - 255) < 32 && Math.abs(data[i + 1] - 192) < 32 && Math.abs(data[i + 2] - 77) < 40) count += 1;
		}
		return count;
	}, deck);
}

/** Pixels of a deck's plan band, read back from the row. The token must itself read red on 1 and green on 2: a skin
 *  that grays it would otherwise match its own gray and pass. */
function _bandEdgePixels(page: Page, deck: number): Promise<number> {
	return page.evaluate((id) => {
		const canvas = document.querySelector<HTMLCanvasElement>(`.rb-waverow[data-deck="${id}"] canvas.wave-seek-canvas`)!;
		const swatch = document.createElement('canvas').getContext('2d', { willReadFrequently: true })!;
		swatch.fillStyle = getComputedStyle(canvas).getPropertyValue(id === 1 ? '--rb-plan-out' : '--rb-plan-in').trim();
		swatch.fillRect(0, 0, 1, 1);
		const [r, g, b] = swatch.getImageData(0, 0, 1, 1).data;
		if (id === 1 ? r < g + 60 : g < r + 60) return 0;
		const { data } = canvas.getContext('2d')!.getImageData(0, 0, canvas.width, canvas.height);
		let count = 0;
		for (let i = 0; i < data.length; i += 4) {
			if (Math.abs(data[i] - r) < 16 && Math.abs(data[i + 1] - g) < 16 && Math.abs(data[i + 2] - b) < 16) count += 1;
		}
		return count;
	}, deck);
}

const incomingStart = (plan: AgentPlan | null) => plan?.overlay?.decks.find((row) => row.role === 'incoming')?.start_ms ?? null;

test('a scheduled plan shows on both decks, follows the incoming deck, and goes when it ends', async ({ page, request }) => {
	test.setTimeout(90_000);
	const [outgoing, incoming] = await _tracks(request);
	await _seedGrid(page);
	await page.setViewportSize({ width: 1280, height: 800 });
	const claiming = page.waitForRequest(
		(req) => req.method() === 'GET' && /\/api\/v1\/commands\/next(?:\?|$)/.test(req.url()),
		{ timeout: 20_000 }
	);
	await page.goto('/performance');
	await expect(page.getByLabel('Open DJ launch animation')).toBeHidden({ timeout: 20_000 });
	await page.waitForFunction(() => window.musicDjToolsPerformance !== undefined, null, { timeout: 60_000 });
	await claiming;
	await _orderSucceeds(page, request, { single: { type: 'autoplay', enabled: false, by_user: true } });
	await page.evaluate(
		async ([a, b]) => {
			await window.musicDjToolsPerformance!.dispatch({ type: 'load', deck: 1, stable_id: a });
			await window.musicDjToolsPerformance!.dispatch({ type: 'load', deck: 2, stable_id: b });
			await window.musicDjToolsPerformance!.dispatch({ type: 'master', deck: 1 });
			await window.musicDjToolsPerformance!.dispatch({ type: 'play', deck: 1, playing: true });
		},
		[outgoing, incoming]
	);
	await expect(page.locator('[data-plan-chip]'), 'no plan, nothing drawn').toHaveCount(0);
	// Ten seconds before the plan's start, so its span is inside the 24 s window on the clock deck.
	await page.evaluate(async () => {
		await window.musicDjToolsPerformance!.dispatch({ type: 'seek', deck: 1, position_ms: 30_000, exact: true });
	});
	await expect
		.poll(() =>
			page.evaluate(() => {
				const { decks } = window.musicDjToolsPerformance!.query();
				return [decks[1].playing, decks[1].beatgrid.length, decks[2].beatgrid.length];
			})
		)
		.toEqual([true, GRID_BEATS, GRID_BEATS]);

	const cueMs = 2 * BAR_MS;
	const order = {
		plan: {
			id: PLAN_ID,
			clock: { deck: 1, stable_id: outgoing },
			incoming: { deck: 2, stable_id: incoming },
			start_bar: 21,
			bars: 4,
			style: 'smoke',
			prepare: [{ type: 'seek', deck: 2, position_ms: cueMs, exact: true }],
			events: [
				{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
				{ at_bar: 0, ramp: { command: { type: 'fader', deck: 2 }, to: 1, over_bars: 2 } },
				{ at_bar: 2, ramp: { command: { type: 'fader', deck: 1 }, to: 0, over_bars: 2 } }
			],
			notes: []
		}
	};
	await _orderSucceeds(page, request, order);

	await expect
		.poll(
			async () => {
				const plan = await _agentPlan(request);
				return JSON.stringify([plan?.id, plan?.status, plan?.reason]);
			},
			{ timeout: 5_000 }
		)
		.toBe(JSON.stringify([PLAN_ID, 'scheduled', null]));
	await expect(page.locator('.rb-waverow[data-deck="1"] [data-plan-chip="clock"]')).toBeVisible();
	await expect(page.locator('.rb-waverow[data-deck="2"] [data-plan-chip="incoming"]')).toBeVisible();
	await expect
		.poll(
			async () => {
				const plan = await _agentPlan(request);
				return JSON.stringify([plan?.status, plan?.reason, plan?.overlay?.decks.map((row) => [row.role, row.start_ms, row.unavailable])]);
			},
			{ timeout: 5_000 }
		)
		.toBe(JSON.stringify(['scheduled', null, [['clock', 40_000, null], ['incoming', cueMs, null]]]));
	const planned = await _agentPlan(request);
	expect(planned!.overlay!.decks.map((row) => [row.role, row.curves.map((curve) => curve.name)])).toEqual([
		['clock', ['fader', 'eq_low']],
		['incoming', ['fader', 'eq_low']]
	]);
	await expect.poll(() => _amberPixels(page, 2), { message: 'the incoming row paints the low EQ line' }).toBeGreaterThan(20);
	await expect.poll(() => _amberPixels(page, 1), { message: 'the clock row paints the low EQ line' }).toBeGreaterThan(20);
	await expect.poll(() => _bandEdgePixels(page, 1), { message: 'the outgoing span is marked red' }).toBeGreaterThan(20);
	await expect.poll(() => _bandEdgePixels(page, 2), { message: 'the incoming span is marked green' }).toBeGreaterThan(20);
	await page.locator('.rb-waverow[data-deck="1"]').screenshot({ path: test.info().outputPath('clock-planned.png') });
	await page.locator('.rb-waverow[data-deck="2"]').screenshot({ path: test.info().outputPath('incoming-planned.png') });

	const moveBars = 3;
	await page.evaluate(
		async (position) => {
			await window.musicDjToolsPerformance!.dispatch({ type: 'seek', deck: 2, position_ms: position, exact: true });
		},
		cueMs + moveBars * BAR_MS
	);
	await expect.poll(async () => incomingStart(await _agentPlan(request)), { timeout: 5_000 }).toBe(cueMs + moveBars * BAR_MS);
	expect((await _agentPlan(request))!.overlay!.anchor_moved?.moved_bars).toBe(moveBars);
	const chip = page.locator('.rb-waverow[data-deck="2"] [data-plan-chip="incoming"]');
	await expect(chip).toContainText('moved +3.0');
	await expect(chip).toHaveAttribute('title', /bars from bar/);
	await page.locator('.rb-waverow[data-deck="2"]').screenshot({ path: test.info().outputPath('incoming-moved.png') });

	await _orderSucceeds(page, request, { plan_amend: { id: PLAN_ID, action: 'abort' } });
	await expect(page.locator('[data-plan-chip]')).toHaveCount(0);
	await expect.poll(async () => (await _agentPlan(request))?.overlay ?? null, { timeout: 5_000 }).toBeNull();
	await expect.poll(() => _amberPixels(page, 2), { message: 'the line is gone with the plan' }).toBeLessThan(20);
	await page.evaluate(async () => {
		await window.musicDjToolsPerformance!.dispatch({ type: 'autoplay', enabled: true, by_user: true });
	});
});
