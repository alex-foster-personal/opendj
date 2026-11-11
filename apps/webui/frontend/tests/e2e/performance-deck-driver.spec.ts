/**
 * FIXT-10408: headless deck-state driver over the production command bus.
 */
import { expect, test, type Page } from '@playwright/test';

import { FIXTURE_MANIFEST_PATH } from './playwright.performance.config';
import {
	deckDriver,
	dispatchPerformance,
	expectDeck,
	queryPerformance,
	viaHttp
} from './support/deck-driver';
import {
	fixtureManifestExists,
	primaryFixtureStableId,
	readFixtureManifest
} from './support/fixture-manifest';
import { waitForPerformanceIpc } from './support/performance-ready';

const manifestExists = fixtureManifestExists(FIXTURE_MANIFEST_PATH);
const trackA = manifestExists ? primaryFixtureStableId(FIXTURE_MANIFEST_PATH) : '';
const trackB = manifestExists
	? (readFixtureManifest(FIXTURE_MANIFEST_PATH).tracks[1]?.stable_id ?? trackA)
	: '';

test.describe.configure({ mode: 'serial' });

test.describe('performance deck driver', () => {
	test.skip(!manifestExists, 'real-library mode: no fixture manifest');

	let page: Page;
	let deck1: ReturnType<typeof deckDriver>;
	let deck2: ReturnType<typeof deckDriver>;
	let http2: ReturnType<typeof viaHttp>;

	test.beforeAll(async ({ browser }) => {
		page = await browser.newPage();
		await page.setViewportSize({ width: 1280, height: 800 });
		await page.goto('/performance');
		await waitForPerformanceIpc(page);
		await dispatchPerformance(page, { type: 'master_volume', value: 0.1 });
	});

	test.beforeEach(async ({ request }) => {
		deck1 = deckDriver(page, 1);
		deck2 = deckDriver(page, 2);
		http2 = viaHttp(request, 2);
	});

	test.afterAll(async () => {
		await page.close();
	});

	test.afterEach(async () => {
		await deck1.teardown();
		await deck2.teardown();
		await http2.teardown();
	});

	test('transport loop hot cue quantize', async () => {
		await deck1.load(trackA);
		await deck1.seekMs(12_000);
		await expectDeck(deck1).toBePausedAt(12_000, 50);
		await deck1.play();
		await expectDeck(deck1).toBePlaying();
		await deck1.pause();
		expect((await deck1.snapshot()).position_ms).toBeGreaterThanOrEqual(12_000);
		await deck1.quantize(false);
		expect((await deck1.snapshot()).quantize_enabled).toBe(false);
		await deck1.quantize(true);
		expect((await deck1.snapshot()).quantize_enabled).toBe(true);
		await deck1.seekMs(8_000);
		await deck1.setHotCue('A');
		const afterSave = await deck1.snapshot();
		const slotA = afterSave.hot_cue_slots.find((row) => row.slot === 'A');
		expect(slotA?.cue?.in_ms).toBe(8_000);
		await deck1.seekMs(20_000);
		await deck1.triggerHotCue('A');
		await expectDeck(deck1).toBePausedAt(8_000, 150);
		await deck1.loop(10_000, 12_000);
		await expectDeck(deck1).toBeLooping(10_000, 12_000);
		await deck1.loopExit();
		expect((await deck1.snapshot()).loop_engaged).toBe(false);
	});

	test('mixer fader eq crossfader and teardown restore', async () => {
		const before = await queryPerformance(page);
		const faderBefore = before.mixer.channels[1].fader;
		await deck1.fader(0.25);
		await deck1.eq('low', 0.2);
		await deck1.eq('high', 0.8);
		await deck1.crossfader(0.3);
		const mid = await queryPerformance(page);
		expect(Math.abs(mid.mixer.channels[1].fader - 0.25)).toBeLessThanOrEqual(1e-3);
		expect(Math.abs(mid.mixer.channels[1].eq_low - 0.2)).toBeLessThanOrEqual(1e-3);
		expect(Math.abs(mid.mixer.channels[1].eq_high - 0.8)).toBeLessThanOrEqual(1e-3);
		expect(Math.abs(mid.mixer.crossfader - 0.3)).toBeLessThanOrEqual(1e-3);
		await deck1.teardown();
		const after = await queryPerformance(page);
		expect(Math.abs(after.mixer.channels[1].fader - faderBefore)).toBeLessThanOrEqual(1e-3);
	});

	test('grid verbs beat jump and beat loop', async () => {
		await deck1.load(trackA);
		const snap = await deck1.snapshot();
		test.skip(
			snap.beatgrid_ms.length < 32,
			'no /anlz beatgrid on fixture track; build with MDT_BEATGRID_WEIGHTS or PERFORMANCE_E2E_OWN_BEATGRID=1'
		);
		const anchor = snap.beatgrid_ms[8] ?? 0;
		await deck1.seekMs(anchor);
		const before = (await deck1.snapshot()).position_ms;
		await deck1.beatJump(4);
		expect((await deck1.snapshot()).position_ms).toBeGreaterThan(before + 1);
		await deck1.beatLoop(4);
		const looped = await deck1.snapshot();
		expect(looped.loop?.beat_length).toBe(4);
		await deck1.loopExit();
	});

	test('HTTP single orders agree with page IPC', async () => {
		await http2.load(trackB);
		await http2.seekMs(15_000);
		await expectDeck(deck2).toBePausedAt(15_000, 50);
		await http2.play();
		await expectDeck(deck2).toBePlaying();
		const mirrorPlaying = await http2.mirrorDeck();
		const pagePlaying = await deck2.snapshot();
		expect(mirrorPlaying.stable_id).toBe(pagePlaying.stable_id);
		expect(mirrorPlaying.playing).toBe(pagePlaying.playing);
		await http2.pause();
		expect((await http2.mirrorDeck()).playing).toBe(false);
		expect((await deck2.snapshot()).playing).toBe(false);
	});

	test('bounded wait names the field that never changed', async () => {
		const quick = deckDriver(page, 3, { timeoutMs: 1_500 });
		try {
			await quick.load(trackA);
			await quick.seekMs(12_000);
			await expect(
				quick.awaitSnapshot(
					'probe',
					'position_ms',
					'within 50 ms of 999999',
					(s) => Math.abs(s.position_ms - 999_999) <= 50
				)
			).rejects.toThrow(/position_ms never .* within 1500 ms/);
		} finally {
			await quick.teardown();
		}
	});
});
