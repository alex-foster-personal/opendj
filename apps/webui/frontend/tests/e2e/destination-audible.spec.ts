/**
 * What reaches the speakers, measured at AudioContext.destination on the REAL
 * app graph.
 *
 * WHAT THIS EXISTS TO CATCH. #2085 (commit 8876a2664) moved master's only path
 * to the speakers through the practice blend, whose master gain defaulted to 0
 * (practice mode, no monitor, MIX 0, and MIX is session-only so every reload
 * is 0). Pressing play produced silence while every meter showed full signal,
 * because every meter and the silence watchdog tap `_masterGain`, upstream of
 * the blend and the mute. No test looked downstream of `_masterGain`, and the
 * specs that play audio ran with `?muted=1`, so nothing could see it.
 *
 * HOW. Before any app script runs, `AudioNode.prototype.connect` is wrapped so
 * that any node connected to its context's `destination` is ALSO connected to
 * one AnalyserNode per context. That tap therefore sums exactly what the
 * destination sums: downstream of master gain, the practice blend, split cable,
 * the master mute and any future stage. An AnalyserNode with nothing
 * downstream is still pulled by Chromium's renderer (the engine's own silence
 * watchdog relies on the same property).
 *
 * WHY --mute-audio IS SAFE HERE. Chromium's --mute-audio mutes the audio
 * output stream at the OS sink; the Web Audio render thread and every
 * AnalyserNode keep computing. This spec is itself the proof: it runs with
 * --mute-audio and asserts a non-silent destination RMS, so if that assumption
 * ever stops holding the spec fails loudly on the RMS floor rather than
 * passing vacuously (the mutation control in the PR that added it recorded
 * both outcomes).
 *
 * RUN. Root `playwright.config.ts` (generated real-audio fixture library,
 * engine + vite on the worktree's claimed port pair):
 *   pnpm exec playwright test destination-audible
 *
 * Acceptance:
 *   [if] a playing deck on the default mixer state reaches the master meter but
 *        not the destination [then STOP] the room is silent while meters lie
 *   [if] the master meter itself reads silent [then STOP] the fixture did not
 *        actually play, so the destination number would prove nothing
 *   [if] the app starts muted on fresh storage [then STOP] the check is vacuous
 */
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

const UI_BASE = (
	process.env.PERFORMANCE_E2E_BASE_URL ??
	process.env.PERFORMANCE_E2E_UI_BASE ??
	''
).replace(/\/$/, '');

/** Destination RMS floor. Silence (gain 0 anywhere downstream) reads exactly 0. */
const DESTINATION_RMS_FLOOR = 1e-3;
/** Library UI prefs only (same as zz-autoplay-playlist-switch.spec.ts): the
 * generated fixture tracks must not be hidden as broken links. Mixer state is
 * left at its fresh-storage defaults. */
const PREFS_STORAGE_KEY = 'mdt.rb.ui-prefs.v1';
const TRACK_ROW = '[data-testid="track-row"]';
const SAMPLE_WINDOW_MS = 1_500;
const SAMPLE_INTERVAL_MS = 50;

test.use({
	launchOptions: { args: ['--autoplay-policy=no-user-gesture-required', '--mute-audio'] }
});

type DestTapWindow = Window & { __destTaps?: AnalyserNode[] };

async function installDestinationTap(page: Page): Promise<void> {
	await page.addInitScript(() => {
		const taps = new WeakMap<BaseAudioContext, AnalyserNode>();
		const all: AnalyserNode[] = [];
		(window as DestTapWindow).__destTaps = all;
		const originalConnect = AudioNode.prototype.connect as (
			this: AudioNode,
			destination: AudioNode | AudioParam,
			output?: number,
			input?: number
		) => AudioNode | void;
		function tapFor(context: BaseAudioContext): AnalyserNode {
			let tap = taps.get(context);
			if (tap === undefined) {
				tap = context.createAnalyser();
				tap.fftSize = 2048;
				taps.set(context, tap);
				all.push(tap);
			}
			return tap;
		}
		AudioNode.prototype.connect = function (
			this: AudioNode,
			destination: AudioNode | AudioParam,
			output?: number,
			input?: number
		) {
			const result = originalConnect.call(this, destination, output, input);
			if (destination === this.context.destination) {
				originalConnect.call(this, tapFor(this.context), output ?? 0, 0);
			}
			return result;
		} as typeof AudioNode.prototype.connect;
	});
}

async function loadAndPlayDeck1(page: Page): Promise<void> {
	await page.getByText('All Tracks', { exact: true }).first().click();
	const firstRow = page.locator(TRACK_ROW).first();
	await expect(firstRow).toBeVisible({ timeout: 30_000 });
	const stableId = await firstRow.getAttribute('data-stable-id');
	expect(stableId, 'fixture library must expose a track row').toBeTruthy();
	await page.evaluate(async (sid) => {
		const ipc = window.musicDjToolsPerformance!;
		await ipc.dispatch({ type: 'load', deck: 1, stable_id: sid! });
		await ipc.dispatch({ type: 'play', deck: 1, playing: true });
	}, stableId);
	await page.waitForFunction(() => window.musicDjToolsPerformance!.query().decks[1].playing === true);
}

test('a playing deck on default mixer state is non-silent at AudioContext.destination', async ({
	page
}) => {
	test.setTimeout(90_000);
	await installDestinationTap(page);
	await page.addInitScript((prefsKey) => {
		window.localStorage.setItem(prefsKey, JSON.stringify({ hide_broken_links: false }));
	}, PREFS_STORAGE_KEY);
	await page.setViewportSize({ width: 1280, height: 800 });
	// Deliberately NO ?muted=1: the mute is one of the stages under test.
	await page.goto(`${UI_BASE}/performance`);
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await loadAndPlayDeck1(page);

	const masterMuted = await page.evaluate(
		() => (window as Window & { __mdtMasterMute?: { get(): boolean } }).__mdtMasterMute?.get()
	);
	expect(masterMuted, 'fresh storage must start unmuted or this check is vacuous').toBe(false);

	const meter = page.getByRole('meter', { name: 'master output level, post master gain' });
	await expect(meter).toBeVisible();
	const meterFloorDb = Number(await meter.getAttribute('aria-valuemin'));

	// Let the deck get past its onset ramp before measuring.
	await expect
		.poll(async () => Number(await meter.getAttribute('aria-valuenow')), { timeout: 15_000 })
		.toBeGreaterThan(meterFloorDb);

	const destination = await page.evaluate(
		async ({ windowMs, intervalMs }) => {
			const taps = (window as DestTapWindow).__destTaps ?? [];
			if (taps.length === 0) throw new Error('no node was ever connected to an AudioContext.destination');
			const running = taps.filter((tap) => tap.context.state === 'running');
			if (running.length === 0) {
				throw new Error(`no running AudioContext; states=${taps.map((t) => t.context.state).join(',')}`);
			}
			let maxRms = 0;
			let sumRms = 0;
			let samples = 0;
			const deadline = performance.now() + windowMs;
			while (performance.now() < deadline) {
				for (const tap of running) {
					const buffer = new Float32Array(tap.fftSize);
					tap.getFloatTimeDomainData(buffer);
					let sum = 0;
					for (const value of buffer) sum += value * value;
					const rms = Math.sqrt(sum / buffer.length);
					maxRms = Math.max(maxRms, rms);
					sumRms += rms;
					samples += 1;
				}
				await new Promise((resolve) => setTimeout(resolve, intervalMs));
			}
			return { maxRms, meanRms: sumRms / samples, samples, contexts: running.length };
		},
		{ windowMs: SAMPLE_WINDOW_MS, intervalMs: SAMPLE_INTERVAL_MS }
	);
	const masterMeterDb = Number(await meter.getAttribute('aria-valuenow'));

	const report =
		`destination RMS max=${destination.maxRms.toExponential(3)} mean=${destination.meanRms.toExponential(3)} ` +
		`(floor ${DESTINATION_RMS_FLOOR}, ${destination.samples} samples over ${destination.contexts} context(s)); ` +
		`master meter=${masterMeterDb.toFixed(1)} dBFS (floor ${meterFloorDb})`;
	console.log(`[destination-audible] ${report}`);

	expect(
		masterMeterDb,
		`master meter reads silent, so the deck never played and the destination number proves nothing: ${report}`
	).toBeGreaterThan(meterFloorDb);
	expect(
		destination.maxRms,
		`SPEAKERS SILENT WHILE METERS SHOW SIGNAL: a stage between _masterGain and AudioContext.destination zeroes audio: ${report}`
	).toBeGreaterThan(DESTINATION_RMS_FLOOR);
});
