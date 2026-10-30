import { expect, test, type Page } from '@playwright/test';

/**
 * AGENT-48: the `>|` button runs the planner's order, and the readout and mirror say which path ran.
 *
 * Runs under playwright.autoplay-stall-gate.config.ts (real backend, real throwaway fixture library). The planner's
 * answer and the tracks' beatgrid are doubles: the fixture's sine tracks have no grid or phrases to plan from, and
 * tests/transition_score/test_live.py covers the real pick. The IPC, the plan runner and the readout are the real ones.
 */

const READOUT = '[data-testid="ap-next-path"]';

// A cold dev compile on a loaded runner alone can pass the config's 60 s.
test.setTimeout(240_000);

async function dispatch(page: Page, message: Record<string, unknown>): Promise<void> {
	await page.evaluate(async (command) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		await ipc.dispatch(command as never);
	}, message);
}

/** 120 BPM over a minute, on the anlz payload the deck reads: the plan runner refuses a deck with no downbeats. */
async function serveBeatgrid(page: Page): Promise<void> {
	const beats = Array.from({ length: 120 }, (_, index) => ({ n: (index % 4) + 1, bpm: 120, t: index * 0.5 }));
	await page.route('**/api/v1/tracks/*/anlz*', async (route) => {
		const response = await route.fetch();
		const json = (await response.json()) as { beatgrid?: Record<string, unknown> };
		await route.fulfill({ response, json: { ...json, beatgrid: { ...json.beatgrid, beats } } });
	});
}

async function loadedTracks(page: Page): Promise<[string, string]> {
	const response = await page.request.get('/api/v1/tracks?limit=2');
	expect(response.ok()).toBe(true);
	const { items: rows } = (await response.json()) as { items: { stable_id: string }[] };
	expect(rows.length, 'the fixture library holds two tracks').toBeGreaterThanOrEqual(2);
	return [rows[0].stable_id, rows[1].stable_id];
}

function plannedAnswer(out: string, incoming: string): Record<string, unknown> {
	const plan = {
		id: 'plan-e2e',
		clock: { deck: 1, stable_id: out },
		incoming: { deck: 2, stable_id: incoming },
		start_bar: 12,
		bars: 8,
		style: 'crossfade',
		prepare: [],
		events: [{ at_bar: 8, command: { type: 'fader', deck: 1, value: 0 } }],
		notes: []
	};
	return {
		order: { plan },
		reason: 'intent smooth: crossfade over 8 bars, M 0.40',
		intent: 'smooth',
		pick: { candidate: 'c1', style: 'crossfade', bars: 8, score: 0.4, out_start_bar: 12, rationale: 'e2e' },
		out_bar_now: 1,
		considered: 3,
		rendered: 3,
		propose_ms: 1,
		render_ms: 1,
		scored: true,
		plan_ms: 1
	};
}

test('arming runs the planner order, and with no order the readout says the fixed loop ran', async ({ page }) => {
	await serveBeatgrid(page);
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance !== undefined, undefined, { timeout: 180_000 });
	const [out, incoming] = await loadedTracks(page);
	await dispatch(page, { type: 'load', deck: 1, stable_id: out });
	await dispatch(page, { type: 'load', deck: 2, stable_id: incoming });
	await expect
		.poll(() =>
			page.evaluate(() => {
				const state = window.musicDjToolsPerformance?.query() as { decks: Record<number, { beatgrid: unknown[] }> };
				return state.decks[1].beatgrid.length;
			})
		)
		.toBe(120);
	await dispatch(page, { type: 'play', deck: 1, playing: true });
	await dispatch(page, { type: 'master', deck: 1 });

	await page.route('**/api/v1/dj/transition/plan', (route) => route.fulfill({ json: plannedAnswer(out, incoming) }));
	await dispatch(page, { type: 'auto_play_next_arm' });
	await expect(page.locator(READOUT)).toHaveText('plan: crossfade 8');
	await dispatch(page, { type: 'auto_play_next_cancel' });

	await page.unroute('**/api/v1/dj/transition/plan');
	await page.route('**/api/v1/dj/transition/plan', (route) =>
		route.fulfill({
			json: { ...plannedAnswer(out, incoming), order: null, pick: null, reason: 'nothing eligible' }
		})
	);
	await dispatch(page, { type: 'auto_play_next_arm' });
	await expect(page.locator(READOUT)).toHaveText('fixed loop');
});

test('right-click "Transition out around here" asks for a plan at the click into the chosen deck (AGENT-50)', async ({ page }) => {
	await serveBeatgrid(page);
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance !== undefined, undefined, { timeout: 180_000 });
	const [out, incoming] = await loadedTracks(page);
	await dispatch(page, { type: 'load', deck: 1, stable_id: out });
	await dispatch(page, { type: 'load', deck: 2, stable_id: incoming });
	const durationOf = () => page.evaluate(() => window.musicDjToolsPerformance!.query().decks[1].duration_ms ?? 0);
	await expect.poll(durationOf).toBeGreaterThan(0);
	await dispatch(page, { type: 'play', deck: 1, playing: true });
	await dispatch(page, { type: 'master', deck: 1 });

	const asked: Record<string, unknown>[] = [];
	await page.route('**/api/v1/dj/transition/plan', async (route) => {
		asked.push(route.request().postDataJSON() as Record<string, unknown>);
		await route.fulfill({ json: plannedAnswer(out, incoming) });
	});
	const strip = page.getByTestId('waveform-seek-deck-1');
	await expect(strip).toBeEnabled();
	const box = (await strip.boundingBox())!;
	await strip.click({ button: 'right', position: { x: box.width * 0.4, y: box.height / 2 } });
	// Control: the deck being right-clicked is never offered as its own target.
	await expect(page.getByTestId('quick-draw-transition-out-1')).toHaveCount(0);
	const item = page.getByTestId('quick-draw-transition-out-2');
	await expect(item).toHaveText('Transition out around here to CH2');
	await item.click();

	await expect(page.locator(READOUT)).toHaveText('plan: crossfade 8');
	expect(asked).toHaveLength(1);
	expect(asked[0].out_deck).toBe(1);
	expect(asked[0].in_deck).toBe(2);
	const durationMs = await durationOf();
	expect(Math.abs((asked[0].from_ms as number) - durationMs * 0.4)).toBeLessThan(durationMs * 0.03);
	await dispatch(page, { type: 'auto_play_next_cancel' });
});
