import { expect, test, type Page } from '@playwright/test';

/**
 * AUTOPLAN-04, AUTOPLAN-06: AutoPlay on, the master past its decision point, a plan on the real page runner, with no press.
 *
 * Runs under playwright.autoplay-stall-gate.config.ts (real backend, real throwaway fixture library). The engine's three
 * transition answers and the tracks' beatgrid are doubles, since the fixture's sine tracks have no grid or phrases to plan
 * from: tests/autodj/test_lookahead.py covers the real search. The AutoPlay poll, the deck load, the IPC, the plan runner,
 * and the readout are the real ones. The fixture's tracks are under the lead, so the decision is due at once.
 *
 * This gate's origin cannot publish a mirror (its PUT answers 403), so the mirror twin is covered by
 * tests/webui/test_dj_transition_decide.py. Curves: PR 7896 draws them from the plan this schedules.
 */

test.setTimeout(240_000);

async function dispatch(page: Page, message: Record<string, unknown>): Promise<void> {
	await page.evaluate(async (command) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		await ipc.dispatch(command as never);
	}, message);
}

async function serveBeatgrid(page: Page): Promise<void> {
	const beats = Array.from({ length: 120 }, (_, index) => ({ n: (index % 4) + 1, bpm: 120, t: index * 0.5 }));
	await page.route('**/api/v1/tracks/*/anlz*', async (route) => {
		const response = await route.fetch();
		const json = (await response.json()) as { beatgrid?: Record<string, unknown> };
		await route.fulfill({ response, json: { ...json, beatgrid: { ...json.beatgrid, beats } } });
	});
}

async function fixtureTracks(page: Page): Promise<string[]> {
	const response = await page.request.get('/api/v1/tracks?limit=10');
	expect(response.ok()).toBe(true);
	const { items } = (await response.json()) as { items: { stable_id: string }[] };
	expect(items.length, 'the fixture library holds two tracks').toBeGreaterThanOrEqual(2);
	return items.map((row) => row.stable_id);
}

function planAnswer(out: string, incoming: string): Record<string, unknown> {
	const plan = {
		id: 'plan-early-e2e',
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
		plan_ms: 1,
		saved: false,
		plan: null
	};
}

test('AutoPlay on and the master past its decision point: a plan is scheduled with no press, and the old handoff stays out', async ({ page }) => {
	await serveBeatgrid(page);
	await page.goto('/performance?playlist=all');
	await page.waitForFunction(() => window.musicDjToolsPerformance !== undefined, undefined, { timeout: 180_000 });
	const [out, incoming] = await fixtureTracks(page);
	const window_ = { start_bar: 13, start_ms: 24_000, max_bars: 8, label: 'outro', energy: 0.4, vocal_free_bars: 8, score: 0.7, reason: 'e2e' };
	let shortlist: string[] = [];
	await page.route('**/api/v1/dj/transition/decide', async (route) => {
		shortlist = (route.request().postDataJSON() as { shortlist: string[] }).shortlist;
		await route.fulfill({
			json: {
				track: incoming,
				window: window_,
				style: 'crossfade',
				bars: 8,
				reason: 'crossfade over 8 bars at bar 13 (outro)',
				windows: [window_],
				rejected: [],
				out_bar_now: 2,
				m_measured: false
			}
		});
	});
	await page.route('**/api/v1/dj/transition/prepare', (route) =>
		route.fulfill({ json: { state: 'ready', candidates: 3, reason: '', render_ms: 1, in_track: incoming } })
	);
	const planRequests: { from_ms?: number }[] = [];
	await page.route('**/api/v1/dj/transition/plan', async (route) => {
		planRequests.push(route.request().postDataJSON() as { from_ms?: number });
		await route.fulfill({ json: planAnswer(out, incoming) });
	});

	await dispatch(page, { type: 'load', deck: 1, stable_id: out });
	await expect
		.poll(() =>
			page.evaluate(() => {
				const state = window.musicDjToolsPerformance?.query() as { decks: Record<number, { beatgrid: unknown[] }> };
				return state.decks[1].beatgrid.length;
			})
		)
		.toBe(120);
	await dispatch(page, { type: 'autoplay', enabled: true });
	await dispatch(page, { type: 'play', deck: 1, playing: true });
	await dispatch(page, { type: 'master', deck: 1 });

	await expect(page.locator('[data-testid="ap-next-path"]')).toHaveText('plan: crossfade 8', { timeout: 30_000 });
	expect(shortlist.length, "if AutoPlay's own pick did not reach the engine then broken").toBeGreaterThan(0);
	expect(planRequests, 'one plan, with no press').toHaveLength(1);
	expect(planRequests[0].from_ms, 'the plan is for the decided window').toBe(window_.start_ms);
	const decks = await page.evaluate(() => {
		const state = window.musicDjToolsPerformance?.query() as { decks: Record<number, { stable_id: string | null; playing: boolean }> };
		return state.decks;
	});
	expect(decks[2].stable_id, 'the decided track is on the follower').toBe(incoming);
	expect(decks[2].playing, 'if the old handoff started it beside the plan then broken').toBe(false);
});
