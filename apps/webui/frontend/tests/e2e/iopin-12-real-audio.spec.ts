/**
 * IOPIN-12 acceptance on the REAL app graph: real AudioContext, real stretch
 * worklet, real decode, real engine, in Chromium.
 *
 * The node suites `iopin-12-djio-stereo-fallback.test.mjs` and
 * `iopin-12-stall-rebuild.test.mjs` run against a recording stand-in for Web
 * Audio. They check how the engine strings its calls together and what it
 * does when a build is made to throw; they cannot say that a browser's graph
 * builds, plays or survives a rebuild. This spec is where that is proved.
 *
 * RUN. Root `playwright.config.ts` (generated real-audio fixture library,
 * engine + vite on the worktree's claimed port pair):
 *   pnpm exec playwright test iopin-12-real-audio
 *
 * Requirements:
 *
 * - ✔︎ ✅ 🎯 A djio request on the output this browser really has loads and
 *   plays decks, audible at the destination, and reports what it wired.
 * - ✔︎ ✅ 🎯 The output-stall rebuild replaces the AudioContext under a
 *   playing deck, keeps the deck's track, and the deck is audible on the new
 *   context.
 *
 * Acceptance tests:
 *
 * - [if] ?djio=master12-cue34 on an output with fewer than 4 channels throws,
 *   or leaves a deck that cannot load [then ⛔️] the live failure is back.
 * - [if] the fallback is wired but no surface names it [then ⛔️] the operator
 *   never learns why cue 3/4 is silent.
 * - [if] a deck plays but the destination is silent [then ⛔️] meters lie.
 * - [if] the rebuild keeps the old context, or drops the deck's track
 *   [then ⛔️] a stalled output is not recovered.
 * - [if] the rebuilt graph is silent at its own destination [then ⛔️] the
 *   deck was re-attached to nothing.
 */
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import {
	DESTINATION_RMS_FLOOR,
	installDestinationTap,
	sampleDestinationRms
} from './support/destination-audio-tap';

const DJIO = 'master12-cue34';
const PREFS_STORAGE_KEY = 'mdt.rb.ui-prefs.v1';
const TRACK_ROW = '[data-testid="track-row"]';
const SAMPLE = { windowMs: 1_500, intervalMs: 50 };

interface OutputTopologyMirror {
	requested_profile: string | null;
	active_profile: string | null;
	fallback: { available_channels: number; message: string } | null;
}

test.use({
	launchOptions: { args: ['--autoplay-policy=no-user-gesture-required', '--mute-audio'] }
});

async function openPerformance(page: Page, query: string): Promise<void> {
	await installDestinationTap(page);
	await page.addInitScript((prefsKey) => {
		window.localStorage.setItem(prefsKey, JSON.stringify({ hide_broken_links: false }));
	}, PREFS_STORAGE_KEY);
	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto(`/performance?playlist=all${query}`);
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
}

async function trackIds(page: Page, count: number): Promise<string[]> {
	const rows = page.locator(TRACK_ROW);
	await expect(rows.first()).toBeVisible({ timeout: 30_000 });
	const ids = await rows.evaluateAll((elements) =>
		elements.map((element) => element.getAttribute('data-stable-id'))
	);
	const present = ids.filter((id): id is string => id !== null && id !== '');
	expect(present.length, 'the fixture library must expose enough track rows').toBeGreaterThanOrEqual(count);
	return present.slice(0, count);
}

async function load(page: Page, deck: 1 | 2, stableId: string): Promise<void> {
	await page.evaluate(
		async ({ deckId, sid }) => {
			await window.musicDjToolsPerformance!.dispatch({ type: 'load', deck: deckId, stable_id: sid });
		},
		{ deckId: deck, sid: stableId }
	);
	await page.waitForFunction(
		({ deckId, sid }) => window.musicDjToolsPerformance!.query().decks[deckId].stable_id === sid,
		{ deckId: deck, sid: stableId }
	);
}

async function play(page: Page, deck: 1 | 2): Promise<void> {
	await page.evaluate(async (deckId) => {
		await window.musicDjToolsPerformance!.dispatch({ type: 'play', deck: deckId, playing: true });
	}, deck);
	await page.waitForFunction(
		(deckId) => window.musicDjToolsPerformance!.query().decks[deckId].playing === true,
		deck
	);
}

/** The app's OWN module instance, served by vite from the registry the page
 * loaded, so this reads and drives the live engine rather than a second copy. */
async function outputTopology(page: Page): Promise<OutputTopologyMirror> {
	return page.evaluate(async () => {
		const status = await import(
			new URL('/src/lib/rb/audio-output-status.svelte.ts', location.href).href
		);
		return JSON.parse(JSON.stringify(status.outputTopologyMirror()));
	});
}

/** Every AudioContext the destination tap has seen, oldest first, with its state. */
async function contextStates(page: Page): Promise<string[]> {
	return page.evaluate(() =>
		((window as Window & { __destTaps?: AnalyserNode[] }).__destTaps ?? []).map(
			(tap) => tap.context.state
		)
	);
}

async function expectAudible(page: Page, what: string): Promise<void> {
	// Past the onset ramp: poll until one window is above the floor, bounded.
	await expect
		.poll(async () => (await sampleDestinationRms(page, SAMPLE)).maxRms, {
			timeout: 20_000,
			message: `${what}: silent at AudioContext.destination`
		})
		.toBeGreaterThan(DESTINATION_RMS_FLOOR);
}

test('IOPIN-12: a djio request on this output loads and plays decks and reports what it wired', async ({
	page
}) => {
	test.setTimeout(120_000);
	await openPerformance(page, `&djio=${DJIO}`);
	const [first, second] = await trackIds(page, 2);

	// The live failure: the first load threw, and no deck loaded again.
	await load(page, 1, first);
	await load(page, 2, second);
	await play(page, 1);
	await expectAudible(page, 'deck 1 on a djio page');

	const channels = await page.evaluate(() => {
		const taps = (window as Window & { __destTaps?: AnalyserNode[] }).__destTaps ?? [];
		if (taps.length !== 1) throw new Error(`expected one audio context, found ${taps.length}`);
		const destination = taps[0].context.destination;
		return { max: destination.maxChannelCount, wired: destination.channelCount };
	});
	const topology = await outputTopology(page);
	console.log(`[iopin-12] output maxChannelCount=${channels.max}, topology=${JSON.stringify(topology)}`);
	expect(topology.requested_profile).toBe(DJIO);

	if (channels.max < 4) {
		// The case observed live: laptop speakers selected, djio requested.
		expect(topology.active_profile, 'stereo master is wired, not the 4-channel layout').toBeNull();
		expect(topology.fallback?.available_channels).toBe(channels.max);
		expect(channels.wired, 'the destination is never forced past what the device has').toBeLessThanOrEqual(channels.max);
		// The toast raised at graph build lasts 15 s, and two loads plus the
		// audibility poll can outlast it. The I/O panel notice is the surface that
		// stays for the session, so that is the one asserted here.
		await page.getByRole('button', { name: 'SHOW AUDIO I/O' }).click();
		const notice = page
			.getByRole('dialog', { name: 'Audio I/O settings' })
			.locator('[data-djio-fallback-notice]');
		await expect(
			notice,
			'if no lasting surface names the fallback then cue 3/4 is silent with no reason'
		).toBeVisible();
		await expect(notice).toHaveText(topology.fallback!.message);
		await expect(notice).toHaveText(/Mixtour 4-channel output unavailable/);
	} else if (channels.max >= 4) {
		// A 4-channel device is selected on this host: djio must really be wired.
		expect(topology.active_profile).toBe(DJIO);
		expect(topology.fallback).toBeNull();
		expect(channels.wired).toBe(4);
	}
});

test('IOPIN-12: the output-stall rebuild moves a playing deck onto a new AudioContext, still audible', async ({
	page
}) => {
	test.setTimeout(120_000);
	await openPerformance(page, '');
	const [first] = await trackIds(page, 1);
	await load(page, 1, first);
	await play(page, 1);
	await expectAudible(page, 'deck 1 before the rebuild');
	expect(await contextStates(page), 'control: one live context before the rebuild').toEqual(['running']);

	// The same armed recovery the liveness poll's `stalled` verdict runs.
	await page.evaluate(async () => {
		const instrumentation = await import(
			new URL('/src/lib/rb/audio-context-instrumentation.ts', location.href).href
		);
		await instrumentation._recoverOutputStallForTests();
	});

	const states = await contextStates(page);
	expect(states.length, 'if still one context then nothing was rebuilt').toBe(2);
	expect(states[0], 'the replaced context is closed, not left running beside the new one').toBe('closed');

	const deck = await page.evaluate(() => {
		const state = window.musicDjToolsPerformance!.query().decks[1];
		return { stable_id: state.stable_id, processor_error: state.processor_error ?? null };
	});
	expect(deck.stable_id, 'if the deck lost its track then the rebuild unloaded a healthy deck').toBe(first);
	expect(deck.processor_error).toBeNull();

	await play(page, 1);
	await expect
		.poll(async () => (await contextStates(page))[1], { timeout: 25_000 })
		.toBe('running');
	// The first tap belongs to the closed context and reads zero, so a
	// non-silent sample can only come from the rebuilt graph.
	await expectAudible(page, 'deck 1 on the rebuilt graph');
});
