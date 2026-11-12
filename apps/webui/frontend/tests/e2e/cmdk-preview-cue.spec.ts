/**
 * CMDK-07..08 (issue #6090; Codex r4278602487 on #6687): the Cmd K preview
 * strip and its arrow-key cue preview, driven through the PRODUCTION path.
 *
 * tests/unit/command-bar-v2-preview.test.mjs drives the controller with a
 * recording dispatcher whose commands always succeed, so a regression in the
 * typed dispatcher, the engine's preview voice or the real decode could leave
 * it green. This spec has no stand-in for any of them: the real engine,
 * decoder and fixture library (playwright.cmdk-preview-cue.config.ts), the
 * bar's own key handling, the dispatcher's `preview_cue` / `preview_stop`, and
 * assertions on the real performance state
 * (`window.musicDjToolsPerformance.query().preview`). The downbeats the jump
 * needs are measured by the production librosa analyzer on the fixture's
 * generated audio and served by `/beatgrid-fallback` when `/anlz` is empty
 * (support/cmdk_preview_cue_fixture.py); nothing is routed, rewritten or
 * fabricated.
 *
 * Regression lines:
 * - if → on a highlighted row does not start that track in the cue through the dispatcher then broken
 *   (the positive control: the spec fails when the preview never starts, it never passes on silence)
 * - if the first → does not land on bar 1, or a second → does not move the voice to the next 4-bar stop, then broken
 * - if Esc does not stop a sounding preview, or closing the bar leaves it sounding, then broken
 * - if a cue preview loads, moves or starts a deck then broken
 * - if neither /anlz nor /beatgrid-fallback serves enough downbeats then the gate fails as UNAVAILABLE, naming both
 * - if the bar's /anlz warm-up runs tracked in its effect then the effect reruns on its own cache write,
 *   Svelte aborts with effect_update_depth_exceeded and the preview state is dropped: broken (found by this spec)
 */
import { expect, test, type Page, type Response } from '@playwright/test';

import type { PerformanceState } from '../../src/lib/rb/performance-ipc.svelte';

/** What the fixture's generated tracks are titled after (support/cmdk_preview_cue_fixture.py SEARCH_QUERY). */
const SEARCH_QUERY = 'cmdk-preview-cue';
/** Bars per → (cmdk-preview-grid.ts PREVIEW_STEP_BARS): the second press lands on this bar index. */
const STEP_BARS = 4;
/** A cold first start decodes the whole track before it sounds. */
const PREVIEW_START_MS = 30_000;

interface AnlzBeat {
	n: number;
	t: number;
}

interface GridPayload {
	beatgrid: { source?: string; beats: AnlzBeat[] };
}

async function query(page: Page): Promise<PerformanceState> {
	return page.evaluate(() => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.query();
	});
}

async function deckTracks(page: Page): Promise<Array<string | null>> {
	const state = await query(page);
	return Object.values(state.decks).map((deck) => deck.stable_id);
}

async function stripMessage(page: Page): Promise<string> {
	return (await page.getByTestId('command-bar-preview-message').textContent())?.trim() ?? '';
}

/** Open the v2 bar. It loads on demand after the flags are read, so press until it opens. */
async function openBar(page: Page): Promise<void> {
	await expect(async () => {
		await page.keyboard.press('Control+k');
		await expect(page.locator('#cmdk2-list')).toBeVisible({ timeout: 1_000 });
	}).toPass({ timeout: 30_000 });
	await expect(page.getByTestId('command-bar-input')).toBeFocused();
}

/** → on the highlighted row, then wait for the real preview voice to sound that track. */
async function jumpAndWaitSounding(page: Page, stableId: string): Promise<PerformanceState['preview']> {
	await page.keyboard.press('ArrowRight');
	try {
		await expect
			.poll(
				async () => {
					const preview = (await query(page)).preview;
					return { stable_id: preview.stable_id, playing: preview.playing };
				},
				// Polled often, so the position read right after shows the landing, not seconds of play past it.
				{ timeout: PREVIEW_START_MS, intervals: [50] }
			)
			.toEqual({ stable_id: stableId, playing: true });
	} catch (error) {
		const state = await query(page);
		throw new Error(
			`→ on ${stableId} never started it in the cue: preview ${JSON.stringify(state.preview)}, ` +
				`strip says ${JSON.stringify(await stripMessage(page))}, last_error ${JSON.stringify(state.last_error)}`,
			{ cause: error }
		);
	}
	return (await query(page)).preview;
}

test.describe.configure({ mode: 'serial' });
// The config starts no engine on Windows: its shared launcher runs /bin/sh and .venv/bin/python.
test.skip(process.platform === 'win32', 'POSIX-only: guardedWebServerCommand launches /bin/sh and .venv/bin/python');

test('→ previews the highlighted track in the cue through the dispatcher, Esc and closing the bar stop it, no deck moves', async ({
	page
}) => {
	test.fixme(true, 'fixture track has no downbeats, see #13527');
	// The dev server compiles /performance on this first visit.
	test.setTimeout(240_000);
	// Every grid answer the page gets, so the grid the bar jumps on is read from the real response.
	const anlz = new Map<string, Response>();
	const fallback = new Map<string, Response>();
	page.on('response', (response) => {
		const anlzMatch = /\/api\/v1\/tracks\/([^/]+)\/anlz(?:\?|$)/.exec(response.url());
		if (anlzMatch !== null && response.ok()) anlz.set(decodeURIComponent(anlzMatch[1]), response);
		const fallbackMatch = /\/api\/v1\/tracks\/([^/]+)\/beatgrid-fallback(?:\?|$)/.exec(response.url());
		if (fallbackMatch !== null && response.ok()) fallback.set(decodeURIComponent(fallbackMatch[1]), response);
	});

	// A Svelte runtime error (an effect that reruns on its own write aborts the whole flush and
	// drops the bar's state with it) fails the test by name, not three steps later.
	const svelteErrors: string[] = [];
	page.on('pageerror', (error) => {
		if (error.message.includes('svelte.dev/e/')) svelteErrors.push(error.message);
	});

	// Muted after the silence watchdog's tap, so nothing is audible; the cue keeps its own gain.
	await page.goto('/performance?muted=1', { timeout: 170_000 });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, { timeout: 170_000 });
	const decksBefore = await deckTracks(page);
	expect(await query(page).then((s) => s.preview.stable_id), 'nothing previews before the test').toBeNull();

	await openBar(page);
	await page.getByTestId('command-bar-input').fill(SEARCH_QUERY);
	const rows = page.locator('[data-testid="command-bar-row"]');
	await expect.poll(() => rows.count(), { message: `the bar lists no fixture track for ${SEARCH_QUERY}` }).toBeGreaterThan(1);

	// ↓ then ↑: the arrows mark the list as touched, which is what hands ← → to the preview.
	await page.keyboard.press('ArrowDown');
	await page.keyboard.press('ArrowUp');
	const highlighted = page.locator('[data-testid="command-bar-row"][aria-selected="true"]');
	await expect(highlighted).toHaveCount(1);
	const stableId = await highlighted.getAttribute('data-stable-id');
	expect(stableId, 'the highlighted row carries no data-stable-id').not.toBeNull();
	const id = stableId as string;

	// CMDK-07: the strip above the bar names the highlighted track and draws its overview.
	const strip = page.getByTestId('command-bar-preview');
	await expect(strip).toBeVisible();
	await expect(strip).toContainText(SEARCH_QUERY);
	await expect(strip, 'the strip never drew the overview the engine serves').toHaveAttribute('data-state', 'ready', {
		timeout: 30_000
	});

	// The jump prefers a real /anlz grid, then uses the same /beatgrid-fallback
	// downbeats as the deck waveform when /anlz is empty.
	const readDownbeats = async (response: Response | undefined): Promise<{ payload: GridPayload; downbeats: number[] } | null> => {
		if (response === undefined) return null;
		const payload = (await response.json()) as GridPayload;
		return { payload, downbeats: payload.beatgrid.beats.filter((beat) => beat.n === 1).map((beat) => beat.t) };
	};
	const usableGrid = async (): Promise<{ endpoint: '/anlz' | '/beatgrid-fallback'; downbeats: number[] } | null> => {
		const primary = await readDownbeats(anlz.get(id));
		if (primary !== null && primary.payload.beatgrid.beats.length > 1 && primary.downbeats.length > STEP_BARS)
			return { endpoint: '/anlz', downbeats: primary.downbeats };
		const rescue = await readDownbeats(fallback.get(id));
		if (rescue !== null && rescue.downbeats.length > STEP_BARS)
			return { endpoint: '/beatgrid-fallback', downbeats: rescue.downbeats };
		return null;
	};
	try {
		await expect
			.poll(async () => (await usableGrid())?.endpoint ?? null, {
				timeout: 30_000,
				message: `the bar found no usable /anlz or /beatgrid-fallback grid for ${id}`
			})
			.not.toBeNull();
	} catch (error) {
		const primary = await readDownbeats(anlz.get(id));
		const rescue = await readDownbeats(fallback.get(id));
		throw new Error(
			`UNAVAILABLE: ${id} has ${primary?.downbeats.length ?? 'no'} /anlz downbeats and ` +
				`${rescue?.downbeats.length ?? 'no'} /beatgrid-fallback downbeats; the bar cannot jump by bars`,
			{ cause: error }
		);
	}
	const selectedGrid = await usableGrid();
	if (selectedGrid === null) throw new Error(`UNAVAILABLE: the usable grid for ${id} disappeared`);
	const downbeats = selectedGrid.downbeats;
	// Let the page publish that answer into its cache before the key that reads it.
	await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));

	// CMDK-08, first →: the track starts in the cue at bar 1 (no master plays, so phase 0).
	const first = await jumpAndWaitSounding(page, id);
	expect(svelteErrors, 'the bar raised a Svelte runtime error while previewing').toEqual([]);
	expect(await stripMessage(page)).toContain('Bar 1');
	expect(first.route, 'the preview plays on the cue path').not.toBeNull();
	const barOneMs = downbeats[0] * 1000;
	const barTwoMs = downbeats[1] * 1000;
	const nextStopMs = downbeats[STEP_BARS] * 1000;
	expect(first.position_ms, 'the first → starts at the first downbeat').toBeGreaterThanOrEqual(barOneMs - 100);
	// Inside bar 1, not just before the next stop: a landing on bars 2-4 fails here (Sol review of 8e53c84f1).
	// The read comes one poll after the cue starts sounding, so the voice has played that long at most.
	expect(first.position_ms, 'the first → starts in bar 1, not a later bar').toBeLessThan(barTwoMs);

	// Second →: the voice moves to the next 4-bar stop, which only a real seek through the dispatcher does.
	const pressedAt = Date.now();
	await page.keyboard.press('ArrowRight');
	await expect.poll(() => stripMessage(page)).toContain(`Bar ${STEP_BARS + 1}`);
	await expect
		.poll(async () => (await query(page)).preview.position_ms, {
			timeout: PREVIEW_START_MS,
			message: `the second → never moved the cue to bar ${STEP_BARS + 1} (${nextStopMs} ms)`
		})
		.toBeGreaterThanOrEqual(nextStopMs - 100);
	// Playing on from bar 1 would take most of the way to the stop, so the stop must be reached in under half that
	// time. A press the dispatcher ignored leaves the first preview playing and only gets there by playing on, which
	// a window around the target alone cannot tell from a seek (Sol P1, #6687).
	const naturalMs = nextStopMs - first.position_ms;
	expect(Date.now() - pressedAt, 'the second → reached bar 5 by playing on from bar 1, not by a seek').toBeLessThan(naturalMs / 2);
	expect((await query(page)).preview.position_ms, 'the seek overshot the stop').toBeLessThan(nextStopMs + 1000);
	expect((await query(page)).preview.stable_id).toBe(id);

	// Esc stops the sounding preview before it clears or closes anything.
	await page.keyboard.press('Escape');
	await expect.poll(async () => (await query(page)).preview.stable_id, { message: 'Esc left the preview sounding' }).toBeNull();
	expect((await query(page)).preview.playing).toBe(false);
	await expect(page.locator('#cmdk2-list'), 'the Esc that stopped the preview closed nothing').toBeVisible();

	// Start it again, then close the bar outright: closing must stop it too.
	await jumpAndWaitSounding(page, id);
	await page.getByTestId('command-bar-backdrop').click({ position: { x: 5, y: 5 } });
	await expect(page.locator('#cmdk2-list')).toBeHidden();
	await expect
		.poll(async () => (await query(page)).preview.stable_id, { message: 'closing the bar left the preview sounding' })
		.toBeNull();

	// A cue preview is not a transport action: every deck holds what it held before.
	expect(await deckTracks(page), 'a cue preview moved a deck').toEqual(decksBefore);
	const decks = Object.values((await query(page)).decks);
	expect(decks.some((deck) => deck.playing), 'a cue preview started a deck').toBe(false);
	expect(svelteErrors, 'the bar raised a Svelte runtime error').toEqual([]);
});
