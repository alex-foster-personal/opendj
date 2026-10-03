/**
 * Beat Sync adversarial e2e (round 2). Real engine, real fixture audio, real
 * own beatgrids (Beat This! on the generated audio, see
 * support/deckload_fixture.py `--seed-own-beatgrid`). No mocks.
 *
 * Each test states what a DJ would hear if it fails and asserts the specific
 * number that would be wrong.
 */
import { expect, test } from '@playwright/test';

import {
	dispatch,
	maxAbs,
	ownGridTrack,
	phaseErrorsMs,
	playSyncedPair,
	query,
	rhythmSlipMs,
	sampleAround,
	settledPhaseErrorMs,
	dumpSamples,
	waitAudible
} from './support/beat-sync-e2e';

const MASTER_TITLE = 'webkit-fixture-a-128bpm';
const FOLLOWER_TITLE = 'webkit-fixture-b-124bpm';

test.describe('Beat Sync adversarial (round 2)', () => {
	test('S1: a pitch fader sweep on a synced master lands without seconds of lag', async ({
		page,
		request
	}) => {
		// What a DJ hears if this fails: they move the master's pitch fader
		// (MIDI or on screen) and the tempo keeps creeping for seconds after
		// their hand has stopped, while every follower re-locks once per
		// stale fader message.
		const master = await ownGridTrack(request, MASTER_TITLE);
		const follower = await ownGridTrack(request, FOLLOWER_TITLE);
		await playSyncedPair(page, master, follower);

		// A MIDI fader at ~100 messages/s for 0.3 s: 30 tempo commands, each
		// dispatched without waiting for the previous one, exactly like
		// action-glue's `void dispatchPerformanceCommand(...)`.
		const result = await page.evaluate(async () => {
			const ipc = window.musicDjToolsPerformance!;
			const ratios = Array.from({ length: 30 }, (_, i) => 1 + (i + 1) * 0.001);
			const t0 = performance.now();
			const pending: Promise<unknown>[] = [];
			for (const ratio of ratios) {
				pending.push(ipc.dispatch({ type: 'tempo', deck: 1, ratio }).catch((e) => e));
				await new Promise((resolve) => setTimeout(resolve, 10));
			}
			const sweepEndMs = performance.now() - t0;
			await Promise.all(pending);
			const settledMs = performance.now() - t0;
			const state = ipc.query();
			return {
				sweepEndMs,
				settledMs,
				finalPitch: state.decks[1].pitch,
				queued: state.command_queued
			};
		});
		console.log(`S1 sweep: ${JSON.stringify(result)}`);
		expect(result.finalPitch, 'master lands on the last fader position').toBeCloseTo(1.03, 6);
		// The fader stopped at sweepEndMs. With followers every master tempo
		// awaits one re-anchor ramp (~850 ms measured), so the floor after the
		// last message is the step still running plus the last value: two ramps,
		// under 2 s. Before the fix it was 25.4 s (one ramp per message).
		expect(
			result.settledMs - result.sweepEndMs,
			`tempo settled ${Math.round(result.settledMs)} ms after the sweep began; the sweep ended at ${Math.round(result.sweepEndMs)} ms`
		).toBeLessThan(2000);
		const settled = await query(page);
		expect(settled.decks[2].sync_error).toBeNull();
		expect(await settledPhaseErrorMs(page, 1, 2)).toBeLessThan(20);
	});

	/** Phase error a DJ can hear as a flam. 15 ms is the phase lock's own
	 * re-seek threshold (F3), so anything above it after landing is a lock
	 * that did not hold. */
	const FLAM_MS = 15;
	/** Time allowed for an action to land: the group schedule lead plus a
	 * tick of the phase lock. */
	const LAND_MS = 700;

	function describeErrors(errors: { t: number; err: number }[]): string {
		const worst = errors.reduce((a, b) => (Math.abs(b.err) > Math.abs(a.err) ? b : a), errors[0]);
		return `worst ${worst?.err.toFixed(1)} ms at t=${worst?.t.toFixed(0)} ms over ${errors.length} samples`;
	}

	async function syncedPair(
		page: import('@playwright/test').Page,
		request: import('@playwright/test').APIRequestContext,
		beatSyncMax = true
	) {
		const master = await ownGridTrack(request, MASTER_TITLE);
		const follower = await ownGridTrack(request, FOLLOWER_TITLE);
		await playSyncedPair(page, master, follower, { beatSyncMax });
		// Let the join land and the lock settle before attacking it.
		await expect
			.poll(() => settledPhaseErrorMs(page, 1, 2, { samples: 5, gapMs: 80 }))
			.toBeLessThan(FLAM_MS);
		return { master, follower };
	}

	// The BeatSyncMax pref also persists to the daemon's disk prefs, so an OFF
	// test restores the default for every later spec on the same server.
	test.afterEach(async ({ page }) => {
		const button = page.locator('button.topbar-slot-bsm');
		if ((await button.count()) > 0 && (await button.getAttribute('aria-pressed')) === 'false') {
			await button.click();
			await expect(button).toHaveAttribute('aria-pressed', 'true');
		}
	});

	for (const beatSyncMax of [true, false]) {
		const bsm = beatSyncMax ? 'BeatSyncMax on' : 'BeatSyncMax off';
		test(`master quantized seek keeps the master rhythm and the follower phase (${bsm})`, async ({
			page,
			request
		}) => {
			// A DJ clicks the MASTER's waveform (or presses CUE-jump) with quantize
			// on (both BeatSyncMax states: ON is the default and takes the
			// group `master-max` schedule, OFF free-seeks the master). Rekordbox lands the jump on the same beat
			// phase it left, so the groove does not skip and synced decks stay in
			// phase. Failure here is heard as a skip on the master and a flam on
			// the follower until the lock re-seeks it.
			const { master } = await syncedPair(page, request, beatSyncMax);
			const now = (await query(page)).decks[1].position_ms;
			const target = master.beatgrid_ms.find((t) => t > now + 8000)!;
			const { samples, errors } = await sampleAround(page, [
				{
					type: 'seek',
					deck: 1,
					position_ms: target + 0.37 * (master.beatgrid_ms[1] - master.beatgrid_ms[0])
				}
			]);
			dumpSamples('master seek', samples);
			expect(errors).toEqual([]);
			const slip = rhythmSlipMs(samples, 1, LAND_MS);
			expect(
				maxAbs(slip),
				`master rhythm slip after a quantized seek: ${slip.slice(0, 3).map((v) => v.toFixed(1))}`
			).toBeLessThan(5);
			const after = phaseErrorsMs(samples, 1, 2).filter((e) => e.t >= LAND_MS);
			expect(maxAbs(after.map((e) => e.err)), describeErrors(after)).toBeLessThan(FLAM_MS);
		});

		test(`master beat jump keeps the follower in phase (${bsm})`, async ({ page, request }) => {
			await syncedPair(page, request, beatSyncMax);
			const { samples, errors } = await sampleAround(page, [
				{ type: 'beat_jump', deck: 1, beats: 4 }
			]);
			dumpSamples('master beat jump', samples);
			expect(errors).toEqual([]);
			expect(
				maxAbs(rhythmSlipMs(samples, 1, LAND_MS)),
				'master rhythm slip after +4 beats'
			).toBeLessThan(5);
			const after = phaseErrorsMs(samples, 1, 2).filter((e) => e.t >= LAND_MS);
			expect(maxAbs(after.map((e) => e.err)), describeErrors(after)).toBeLessThan(FLAM_MS);
		});
	}

	test('a paused follower that plays again rejoins in phase', async ({ page, request }) => {
		await syncedPair(page, request);
		await dispatch(page, { type: 'play', deck: 2, playing: false });
		await page.waitForTimeout(1300);
		const { samples, errors } = await sampleAround(
			page,
			[{ type: 'play', deck: 2, playing: true }],
			{
				beforeMs: 100
			}
		);
		expect(errors).toEqual([]);
		const after = phaseErrorsMs(samples, 1, 2).filter(
			(e) => e.t >= LAND_MS && samples.find((s) => s.t === e.t)!.playing[2]
		);
		expect(after.length).toBeGreaterThan(10);
		expect(maxAbs(after.map((e) => e.err)), describeErrors(after)).toBeLessThan(FLAM_MS);
		expect((await query(page)).decks[2].sync_error).toBeNull();
	});

	test('a 4-beat loop on the master and its exit keep the follower in phase', async ({
		page,
		request
	}) => {
		await syncedPair(page, request);
		const engaged = await sampleAround(page, [{ type: 'beat_loop', deck: 1, beats: 4 }], {
			afterMs: 4500
		});
		expect(engaged.errors).toEqual([]);
		const looped = phaseErrorsMs(engaged.samples, 1, 2).filter((e) => e.t >= LAND_MS);
		expect(
			maxAbs(looped.map((e) => e.err)),
			`while looping: ${describeErrors(looped)}`
		).toBeLessThan(FLAM_MS);
		expect((await query(page)).decks[1].loop?.engaged).toBe(true);
		const exited = await sampleAround(page, [{ type: 'loop', deck: 1, loop: null }]);
		expect(exited.errors).toEqual([]);
		const after = phaseErrorsMs(exited.samples, 1, 2).filter((e) => e.t >= LAND_MS);
		expect(
			maxAbs(after.map((e) => e.err)),
			`after loop exit: ${describeErrors(after)}`
		).toBeLessThan(FLAM_MS);
	});

	test('a 4-beat loop on the follower stays in phase with the master', async ({
		page,
		request
	}) => {
		await syncedPair(page, request);
		const { samples, errors } = await sampleAround(
			page,
			[{ type: 'beat_loop', deck: 2, beats: 4 }],
			{
				afterMs: 4500
			}
		);
		expect(errors).toEqual([]);
		const after = phaseErrorsMs(samples, 1, 2).filter((e) => e.t >= LAND_MS);
		expect(maxAbs(after.map((e) => e.err)), describeErrors(after)).toBeLessThan(FLAM_MS);
	});

	test('slip loop on the master returns to the slip position in phase', async ({
		page,
		request
	}) => {
		await syncedPair(page, request);
		await dispatch(page, { type: 'slip', deck: 1, enabled: true });
		await dispatch(page, { type: 'beat_loop', deck: 1, beats: 2 });
		await page.waitForTimeout(2200);
		expect((await query(page)).decks[1].slip_active).toBe(true);
		const { samples, errors } = await sampleAround(page, [{ type: 'loop', deck: 1, loop: null }]);
		dumpSamples('slip return', samples);
		expect(errors).toEqual([]);
		expect(
			maxAbs(rhythmSlipMs(samples, 1, LAND_MS)),
			'master rhythm slip on slip return'
		).toBeLessThan(5);
		const after = phaseErrorsMs(samples, 1, 2).filter((e) => e.t >= LAND_MS);
		expect(maxAbs(after.map((e) => e.err)), describeErrors(after)).toBeLessThan(FLAM_MS);
	});

	test('a key nudge on a locked follower keeps it in phase', async ({ page, request }) => {
		await syncedPair(page, request);
		const { samples, errors } = await sampleAround(page, [
			{ type: 'key_nudge', deck: 2, semitones: 1 }
		]);
		expect(errors).toEqual([]);
		const after = phaseErrorsMs(samples, 1, 2).filter((e) => e.t >= LAND_MS);
		expect(maxAbs(after.map((e) => e.err)), describeErrors(after)).toBeLessThan(FLAM_MS);
	});

	test('Master Tempo toggled on a locked follower keeps it in phase', async ({ page, request }) => {
		await syncedPair(page, request);
		const enabled = (await query(page)).decks[2].master_tempo_enabled;
		const { samples, errors } = await sampleAround(page, [
			{ type: 'master_tempo', deck: 2, enabled: !enabled }
		]);
		expect(errors).toEqual([]);
		const after = phaseErrorsMs(samples, 1, 2).filter((e) => e.t >= LAND_MS);
		expect(maxAbs(after.map((e) => e.err)), describeErrors(after)).toBeLessThan(FLAM_MS);
	});

	test('a pitch range change then a tempo move on the master is followed', async ({
		page,
		request
	}) => {
		await syncedPair(page, request);
		await dispatch(page, { type: 'pitch_range', deck: 1, range: 16 });
		const { samples, errors } = await sampleAround(page, [{ type: 'tempo', deck: 1, ratio: 1.12 }]);
		dumpSamples('range then tempo', samples);
		expect(errors).toEqual([]);
		const state = await query(page);
		expect(state.decks[1].pitch).toBeCloseTo(1.12, 6);
		// 124 BPM following 128 * 1.12 = 143.4 BPM needs +15.6%, beyond the
		// follower's 8% range: the follower must say so, not drift silently.
		if (state.decks[2].sync_error === null) {
			// OPEN FINDING (round 2, not fixed): after a 12% master step the
			// follower's re-anchor ramp lands with a 13-18 ms residual that the
			// 0.3% trim then walks back slowly, so this is red on some runs.
			const after = phaseErrorsMs(samples, 1, 2).filter((e) => e.t >= LAND_MS);
			expect(maxAbs(after.map((e) => e.err)), describeErrors(after)).toBeLessThan(FLAM_MS);
			expect(Math.abs(state.decks[2].effective_bpm! - state.decks[1].effective_bpm!)).toBeLessThan(
				0.1 + 0.003 * state.decks[1].effective_bpm!
			);
		} else {
			expect(state.decks[2].sync_error).toMatch(/range|tempo/i);
		}
	});

	test('unloading a locked follower and loading another track rejoins in phase', async ({
		page,
		request
	}) => {
		await syncedPair(page, request);
		const replacement = await ownGridTrack(request, 'webkit-fixture-c-128bpm-chain');
		await dispatch(page, { type: 'play', deck: 2, playing: false });
		await dispatch(page, { type: 'unload', deck: 2 });
		await dispatch(page, {
			type: 'load',
			deck: 2,
			stable_id: replacement.stable_id
		});
		await expect
			.poll(async () => (await query(page)).decks[2].beatgrid_ms.length)
			.toBeGreaterThan(31);
		expect((await query(page)).decks[2].beat_sync_enabled, 'Beat Sync survives a reload').toBe(
			true
		);
		await dispatch(page, {
			type: 'seek',
			deck: 2,
			position_ms: replacement.beatgrid_ms[16]
		});
		const { samples, errors } = await sampleAround(
			page,
			[{ type: 'play', deck: 2, playing: true }],
			{
				beforeMs: 100
			}
		);
		expect(errors).toEqual([]);
		const after = phaseErrorsMs(samples, 1, 2).filter((e) => e.t >= LAND_MS);
		expect(maxAbs(after.map((e) => e.err)), describeErrors(after)).toBeLessThan(FLAM_MS);
	});

	test('unloading the master hands the follower a working lock or a free run, never a stale one', async ({
		page,
		request
	}) => {
		await syncedPair(page, request);
		await dispatch(page, { type: 'unload', deck: 1 });
		await waitAudible(page, 2);
		const state = await query(page);
		// Deck 2 is the only playing deck: it is master now, so it has nothing
		// to follow and must not carry a sync error or a lock-held tempo trim.
		expect(state.master_deck).toBe(2);
		expect(state.decks[2].sync_error).toBeNull();
		expect(state.decks[2].playing).toBe(true);
	});
});
