/**
 * PVCUE-01 runtime acceptance: a library audition in the headphones goes
 * SILENT the moment a deck load is accepted, measured on the real cue bus.
 *
 * `tests/unit/preview-cue-stops-on-deck-load.test.mjs` pins where the silence
 * sits in `_execute`; it reads source text and cannot say anything is quiet.
 * This spec drives the production path end to end in Chromium: the generated
 * real-audio fixture library (root `playwright.config.ts`), a real
 * `preview_cue` decode into the app's own AudioContext, a real `load` through
 * `window.musicDjToolsPerformance.dispatch` (the production dispatcher and its
 * `_execute`), and an AnalyserNode fanned off the app's live `cueSum` node -
 * the ONLY node the preview player connects to (`preview-cue.svelte.ts`
 * `_ensureGain`). The tap is a measurement fan-out added beside the app's own
 * connections; it changes no route and replaces no node.
 *
 * RUN: pnpm exec playwright test preview-cue-stops-on-deck-load
 *
 * Acceptance:
 * - [if] a deck load is accepted while an audition sounds [then the cue bus
 *   still carries it] the audition plays over the deck the DJ just loaded - broken
 * - [if] a load onto the playing master is refused [then the cue bus goes
 *   silent] a refused click cuts the DJ's audition for nothing - broken
 * - [if] a deck load is accepted while a preview request is still starting
 *   the audio graph [then the audition starts afterwards] the load that was
 *   meant to silence it is overtaken by the cold-start preview - broken
 *
 * HOW THE RACE WINDOW IS FORCED (third test), with nothing in the page
 * replaced or wrapped: `previewCueSeek` awaits `ensureAudioGraphForCue()`,
 * which awaits the real `AudioContext.resume()` whenever the app's context is
 * suspended. The test suspends the app's own context through the real
 * `suspend()` API and dispatches `preview_cue` over the production IPC. Once
 * the dispatcher's history shows it, and one more task has drained, the
 * preview is parked on the real resume (an audio-thread round trip); the test
 * asserts the context is still suspended and nothing plays, then dispatches
 * the deck `load`, which reaches `silencePreviewCue()` before that resume
 * settles. A `superseded` outcome is the proof the window was hit:
 * with the fix, that is the only way the request can end up superseded. The
 * pre-fix source (generation claimed after that await) plays it instead.
 *
 * Why not Chromium's autoplay policy (tried, Thu 15 Oct 2026): every
 * Playwright evaluate runs with CDP `userGesture: true`, so the page is
 * activated before the first IPC call; driving the page over raw CDP with
 * `userGesture: false` keeps it unactivated, but in this Chromium build
 * `--autoplay-policy=user-gesture-required` creates the context RUNNING, and
 * under the default or `document-user-activation-required` a later real
 * click does NOT settle an already pending `resume()`: the request ends on
 * the product's 2.5 s IO timeout instead, which refuses the preview on both
 * the fixed and the pre-fix source, so it cannot tell them apart.
 */
import { expect, test, type Page } from '@playwright/test';

import type { PerformanceCommand, PerformanceState } from '../../src/lib/rb/performance-ipc.svelte';

const TRACK_ROW = '[data-testid="track-row"]';
/** Cue-bus RMS floor. A stopped source contributes exactly 0. */
const CUE_RMS_FLOOR = 1e-3;
const SAMPLE = { windowMs: 1_200, intervalMs: 40 };

type CueTapWindow = Window & { __pvcueCueTap?: AnalyserNode };
async function _query(page: Page): Promise<PerformanceState> {
	return page.evaluate(() => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.query();
	});
}

async function _dispatch(page: Page, command: PerformanceCommand): Promise<void> {
	await page.evaluate(async (cmd) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		await ipc.dispatch(cmd);
	}, command);
}

/** Dispatch and return the rejection message, or null when it resolved. */
async function _dispatchSettled(page: Page, command: PerformanceCommand): Promise<string | null> {
	return page.evaluate(async (cmd) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		try {
			await ipc.dispatch(cmd);
			return null;
		} catch (exc) {
			return exc instanceof Error ? exc.message : String(exc);
		}
	}, command);
}

async function _stableIds(page: Page, count: number): Promise<string[]> {
	await expect.poll(async () => page.locator(TRACK_ROW).count(), { timeout: 30_000 }).toBeGreaterThanOrEqual(count);
	const ids = await page
		.locator(TRACK_ROW)
		.evaluateAll((rows) => rows.map((row) => row.getAttribute('data-stable-id')));
	const present = ids.filter((id): id is string => id !== null && id !== '');
	expect(present.length, 'the fixture library must expose enough track rows').toBeGreaterThanOrEqual(count);
	return present.slice(0, count);
}

/**
 * Fan an AnalyserNode off the app's live cue bus. Imported from the URL vite
 * served the page, so this is the app's OWN module instance and its real node.
 */
async function _tapCueBus(page: Page): Promise<void> {
	await page.evaluate(async () => {
		const headphones = await import(new URL('/src/lib/player/headphones.ts', location.href).href);
		const bus = headphones.peekCueBus() as { context: AudioContext; cueSum: GainNode } | null;
		if (bus === null) throw new Error('the cue bus is not built: no preview or deck has started the graph');
		const w = window as CueTapWindow;
		if (w.__pvcueCueTap !== undefined && w.__pvcueCueTap.context === bus.context) return;
		const tap = bus.context.createAnalyser();
		tap.fftSize = 2048;
		bus.cueSum.connect(tap);
		w.__pvcueCueTap = tap;
	});
}

async function _cueRms(page: Page): Promise<{ maxRms: number; samples: number; state: string }> {
	return page.evaluate(async ({ windowMs, intervalMs }) => {
		const tap = (window as CueTapWindow).__pvcueCueTap;
		if (tap === undefined) throw new Error('cue bus tap is not installed');
		if (tap.context.state !== 'running') {
			throw new Error(`cue bus AudioContext is ${tap.context.state}, cannot measure`);
		}
		let maxRms = 0;
		let samples = 0;
		const deadline = performance.now() + windowMs;
		const buffer = new Float32Array(tap.fftSize);
		while (performance.now() < deadline) {
			tap.getFloatTimeDomainData(buffer);
			let sum = 0;
			for (const value of buffer) sum += value * value;
			maxRms = Math.max(maxRms, Math.sqrt(sum / buffer.length));
			samples += 1;
			await new Promise((resolve) => setTimeout(resolve, intervalMs));
		}
		return { maxRms, samples, state: tap.context.state };
	}, SAMPLE);
}

/** Record a measured window on the test, so a run's report carries the numbers. */
function _note(label: string, rms: { maxRms: number; samples: number }): void {
	test.info().annotations.push({ type: 'cue-rms', description: `${label}: max ${rms.maxRms.toExponential(3)} over ${rms.samples} windows` });
}

async function _expectCueAudible(page: Page, message: string): Promise<void> {
	let last = { maxRms: 0, samples: 0 };
	await expect
		.poll(async () => (last = await _cueRms(page)).maxRms, { timeout: 20_000, message })
		.toBeGreaterThan(CUE_RMS_FLOOR);
	_note('audition sounding', last);
}

async function _startAudition(page: Page, stable_id: string): Promise<void> {
	await _dispatch(page, { type: 'preview_cue', stable_id, ratio: 0.3 });
	await expect.poll(async () => (await _query(page)).preview.playing, { timeout: 30_000 }).toBe(true);
	await _tapCueBus(page);
}

async function _openPerformance(page: Page): Promise<void> {
	// ?muted=1 mutes the room after the silence watchdog's tap, so a deck can
	// play without tripping AUDIO CUT. The cue bus is upstream of that mute.
	await page.goto('/performance?muted=1');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await page.locator('body').click({ position: { x: 2, y: 2 } });
}

test('PVCUE-01: an accepted deck load silences a sounding audition on the real cue bus', async ({ page }) => {
	test.setTimeout(120_000);
	await _openPerformance(page);
	const [auditioned] = await _stableIds(page, 1);

	await _startAudition(page, auditioned);
	await _expectCueAudible(page, 'precondition: the audition must be audible on the cue bus before the load');

	// The real gesture: the DJ auditions a track, then puts it on a deck.
	await _dispatch(page, { type: 'load', deck: 2, stable_id: auditioned });
	await expect.poll(async () => (await _query(page)).decks[2].stable_id, { timeout: 45_000 }).toBe(auditioned);

	const state = await _query(page);
	expect(state.preview.playing, 'an accepted load must stop the audition').toBe(false);
	expect(state.preview.stable_id).toBeNull();
	expect(state.decks[2].playing, 'the loaded deck is idle, so the cue bus has no other source').toBe(false);
	const after = await _cueRms(page);
	_note('after accepted load', after);
	expect(after.samples).toBeGreaterThan(5);
	expect(
		after.maxRms,
		'if a deck load is accepted while an audition sounds and the cue bus still carries it, the audition plays over the deck the DJ just loaded - broken'
	).toBeLessThanOrEqual(CUE_RMS_FLOOR);

	await _dispatch(page, { type: 'unload', deck: 2 });
});

test('PVCUE-01 control: a refused load onto the playing master keeps the audition audible', async ({ page }) => {
	test.setTimeout(150_000);
	await _openPerformance(page);
	const [auditioned, onAir] = await _stableIds(page, 2);

	await _dispatch(page, { type: 'load', deck: 1, stable_id: onAir });
	await expect.poll(async () => (await _query(page)).decks[1].duration_ms, { timeout: 45_000 }).not.toBeNull();
	await _dispatch(page, { type: 'play', deck: 1, playing: true });
	await expect.poll(async () => (await _query(page)).decks[1].playing).toBe(true);
	await expect.poll(async () => (await _query(page)).decks[1].is_master, { timeout: 10_000 }).toBe(true);

	// Baseline: with the master playing and no audition, the cue bus is silent,
	// so any level measured later is the audition and not the deck's PFL.
	await _tapCueBus(page);
	const baseline = await _cueRms(page);
	_note('master playing, no audition', baseline);
	expect(baseline.maxRms, 'the playing master must not reach the cue bus on its own').toBeLessThanOrEqual(CUE_RMS_FLOOR);

	await _startAudition(page, auditioned);
	await _expectCueAudible(page, 'precondition: the audition must be audible on the cue bus');

	// BrowserPanel's destructive replace opts into refuseIfMaster; onto the
	// live master it is refused by the dispatcher.
	const refusal = await _dispatchSettled(page, { type: 'load', deck: 1, stable_id: auditioned, refuseIfMaster: true });
	expect(refusal, 'the load onto the live master must be refused').toMatch(/live master/);

	const state = await _query(page);
	expect(state.decks[1].stable_id, 'the refused load must leave the master deck as it was').toBe(onAir);
	expect(
		state.preview.playing,
		'if a refused master load stops the audition, a refused click cuts the DJ\'s audition for nothing - broken'
	).toBe(true);
	expect(state.preview.stable_id).toBe(auditioned);
	const after = await _cueRms(page);
	_note('after refused master load', after);
	expect(
		after.maxRms,
		'if a refused master load silences the cue bus, a refused click cuts the DJ\'s audition for nothing - broken'
	).toBeGreaterThan(CUE_RMS_FLOOR);

	await _dispatch(page, { type: 'preview_stop' });
	await _dispatch(page, { type: 'play', deck: 1, playing: false });
	await _dispatch(page, { type: 'unload', deck: 1 });
});

test('PVCUE-01 control: a load the engine refuses onto a playing deck keeps the audition audible (Codex P2 4287743996)', async ({ page }) => {
	test.setTimeout(150_000);
	await _openPerformance(page);
	const [auditioned, onAir] = await _stableIds(page, 2);

	await _dispatch(page, { type: 'load', deck: 1, stable_id: onAir });
	await expect.poll(async () => (await _query(page)).decks[1].duration_ms, { timeout: 45_000 }).not.toBeNull();
	await _dispatch(page, { type: 'play', deck: 1, playing: true });
	await expect.poll(async () => (await _query(page)).decks[1].playing).toBe(true);

	await _startAudition(page, auditioned);
	await _expectCueAudible(page, 'precondition: the audition must be audible on the cue bus');

	// No refuseIfMaster: the dispatcher lets it through, and the Web Audio
	// engine refuses to replace a playing deck.
	const refusal = await _dispatchSettled(page, { type: 'load', deck: 1, stable_id: auditioned });
	expect(refusal, 'a load onto a playing deck must be refused').toMatch(/must be fully stopped before replacement/);

	const state = await _query(page);
	expect(state.decks[1].stable_id, 'the refused load must leave the playing deck as it was').toBe(onAir);
	expect(
		state.preview.playing,
		'if a load the engine refuses stops the audition, a refused load cuts the DJ\'s audition for nothing - broken'
	).toBe(true);
	const after = await _cueRms(page);
	_note('after a load refused onto a playing deck', after);
	expect(after.maxRms, 'the cue bus must still carry the audition').toBeGreaterThan(CUE_RMS_FLOOR);

	await _dispatch(page, { type: 'preview_stop' });
	await _dispatch(page, { type: 'play', deck: 1, playing: false });
	await _dispatch(page, { type: 'unload', deck: 1 });
});

test('PVCUE-01: a load accepted while the preview request is still starting the graph keeps it from ever sounding', async ({ page }) => {
	test.setTimeout(150_000);
	await _openPerformance(page);
	const [auditioned, other] = await _stableIds(page, 2);

	// Warm the graph, the tap and the decode cache, then stop: with the buffer
	// cached, the parked request would play the moment resume() settles if
	// nothing cancelled it, so the race has no decode delay to hide behind.
	await _startAudition(page, auditioned);
	await _expectCueAudible(page, 'precondition: the audition must be audible once');
	await _dispatch(page, { type: 'preview_stop' });
	await expect.poll(async () => (await _cueRms(page)).maxRms).toBeLessThanOrEqual(CUE_RMS_FLOOR);

	const loadsBefore = (await _query(page)).history.filter((event) => event.type === 'load').length;
	const raced = await page.evaluate(
		async ({ previewSid, loadSid }) => {
			const tap = (window as CueTapWindow).__pvcueCueTap;
			if (tap === undefined) throw new Error('cue bus tap is not installed');
			const context = tap.context as AudioContext;
			await context.suspend();
			if (context.state !== 'suspended') throw new Error(`context did not suspend: ${context.state}`);
			const ipc = window.musicDjToolsPerformance!;
			const previewsBefore = ipc.query().history.filter((event) => event.type === 'preview_cue').length;
			const preview = ipc
				.dispatch({ type: 'preview_cue', stable_id: previewSid, ratio: 0.3 })
				.then(() => 'started', (exc: unknown) => (exc instanceof Error ? exc.message : String(exc)));
			// Let the preview reach _execute, then drain one task so it is parked
			// on the real resume(); the context must still be suspended then.
			let spins = 0;
			while (ipc.query().history.filter((event) => event.type === 'preview_cue').length === previewsBefore) {
				if (++spins > 200) throw new Error('preview_cue never reached the dispatcher');
				await new Promise((resolve) => setTimeout(resolve, 0));
			}
			await new Promise((resolve) => setTimeout(resolve, 0));
			const stateAtLoad = context.state;
			const playingAtLoad = ipc.query().preview.playing;
			const load = ipc.dispatch({ type: 'load', deck: 2, stable_id: loadSid });
			const outcome = await preview;
			await load;
			return { outcome, stateAtLoad, playingAtLoad, spins };
		},
		{ previewSid: auditioned, loadSid: other }
	);
	test.info().annotations.push({ type: 'raced-preview', description: JSON.stringify(raced) });
	// The window is real only if the load lands while the preview is parked.
	expect(raced.stateAtLoad, 'the load must land while the preview waits on a suspended context').toBe('suspended');
	expect(raced.playingAtLoad, 'the preview must not be playing yet when the load lands').toBe(false);
	expect((await _query(page)).history.filter((event) => event.type === 'load').length).toBeGreaterThan(loadsBefore);
	await expect.poll(async () => (await _query(page)).decks[2].stable_id, { timeout: 45_000 }).toBe(other);

	const state = await _query(page);
	expect(
		state.preview.playing,
		'if a preview request still starting the graph begins after an accepted load, it plays over the deck the DJ just loaded - broken'
	).toBe(false);
	expect(state.preview.stable_id).toBeNull();
	expect(raced.outcome, 'the overtaken preview request must report itself superseded').toBe('superseded');
	await _tapCueBus(page);
	const after = await _cueRms(page);
	_note('after load raced an in-flight preview', after);
	expect(
		after.maxRms,
		'if a preview request still starting the graph begins after an accepted load, the cue bus carries it - broken'
	).toBeLessThanOrEqual(CUE_RMS_FLOOR);

	// Positive control on the same page: a fresh preview is audible, so the
	// silence above is not a dead tap or a dead context.
	await _startAudition(page, auditioned);
	await _expectCueAudible(page, 'control: a fresh preview after the race must be audible');
	await _dispatch(page, { type: 'preview_stop' });
	await _dispatch(page, { type: 'unload', deck: 2 });
});
