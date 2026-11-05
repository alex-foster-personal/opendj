/**
 * AUDIOLIVE-09 amended + AUDIOLIVE-17, on the REAL audio path in Chromium.
 *
 * The master silence watchdog stops every live deck as "AUDIO CUT decks=N"
 * only when the room is silent AND the mixer and stem state PREDICTED sound.
 * Silence the operator chose (fader down, stems muted, a soloed part that is
 * quiet here) or that the track itself holds is content, not a cut.
 *
 * WHY THIS IS A BROWSER TEST (P1 thread r4268111486 on #6636). The first
 * version of this suite fed `noteMasterSilence` a hand-built analyser that
 * always read zero and AudioBuffer-shaped objects full of made-up levels, so it
 * would have stayed green if decoded stem PCM, the analyser tap or the DSP
 * graph delivered something different. AGENTS.md "No mocks and locked real
 * fixtures" forbids exactly that. Here nothing is a stand-in:
 *
 * - real media: committed fixtures (a 60 s mixture with its real vocal and
 *   instrumental stems, and a track with a 3 s silent intro), served through
 *   the production FastAPI routes and decoded by the browser's own
 *   `decodeAudioData`;
 * - the production signal path: engine graph (`StretchDeckProcessor` /
 *   `AlignedStemDeckProcessor` -> `buildDeckChannelGraph`) -> master analyser;
 * - the production gate: `startAppInstruments` registers
 *   `readSilenceSourceDeckSnaps` -> `noteMasterSilence` on the engine RAF loop.
 *
 * Each "excused" case first proves the open graph is audible at the master
 * tap, so the silence it then excuses is caused by the control under test,
 * not by a graph that never carried sound. A dropout is the real thing too:
 * `disconnectDeckProcessorForTest` while the deck is still live.
 *
 * Regression lines:
 * - if a channel fader at 0 (CUE on, pre-listening) stops the deck then broken
 * - if muting every stem stops the deck then broken
 * - if soloing a stem that is silent in this passage stops the deck then broken
 * - if a track's own silent passage stops the deck then broken
 * - if a real dropout (signal expected, none delivered) does NOT stop the deck then broken
 * - if soloing a LOUD stem hides a real dropout then broken
 * - if the live-upgrade envelope scan holds the main thread for a whole part then broken
 *
 * The pure "open part with no envelope never explains silence" law stays in
 * tests/unit/silence-source-pcm.test.mjs: the real processor always takes an
 * envelope per part, so no real graph can produce that input.
 *
 * RUN:
 *   pnpm exec playwright test --config tests/e2e/playwright.silence-expected-audible.config.ts
 */
import { createHash } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

import type {
	Outcome,
	Scenario
} from '../../src/lib/rb/harness/silence-expected-audible-browser-entry';

const MIXTURE_STABLE_ID = 'a'.repeat(40);

const REPO = fileURLToPath(new URL('../../../../../', import.meta.url));
const FIXTURE_MANIFEST_PATH = fileURLToPath(
	new URL('./fixtures/silence-expected-audible.manifest.json', import.meta.url)
);
const HARNESS_MODULE = '/src/lib/rb/harness/silence-expected-audible-browser-entry.ts';

const MANIFEST_SUPPORTED_VERSION = 1;

function verifiedFixtureBytes(relPath: string): Buffer {
	const manifest = JSON.parse(readFileSync(FIXTURE_MANIFEST_PATH, 'utf8')) as {
		version: number;
		files: Record<string, { sha256: string; bytes: number }>;
	};
	if (manifest.version !== MANIFEST_SUPPORTED_VERSION) {
		throw new Error(
			`${FIXTURE_MANIFEST_PATH} declares version ${manifest.version}, this suite supports ` +
				`${MANIFEST_SUPPORTED_VERSION}. Failing closed rather than serving unknown provenance.`
		);
	}
	const entry = manifest.files[relPath];
	if (entry === undefined) {
		throw new Error(`${relPath} is not listed in ${FIXTURE_MANIFEST_PATH}`);
	}
	const abs = REPO + relPath;
	if (!existsSync(abs)) {
		throw new Error(`locked fixture missing: ${abs}`);
	}
	const bytes = readFileSync(abs);
	if (bytes.length !== entry.bytes) {
		throw new Error(
			`${relPath} is ${bytes.length} bytes, manifest says ${entry.bytes}. ` +
				'Canonical media fixtures must remain byte-identical (AGENTS.md fixture contract).'
		);
	}
	const digest = createHash('sha256').update(bytes).digest('hex');
	if (digest !== entry.sha256) {
		throw new Error(
			`${relPath} digest mismatch: expected sha256 ${entry.sha256}, got ${digest}. ` +
				'Canonical media fixtures must remain byte-identical (AGENTS.md fixture contract).'
		);
	}
	return bytes;
}

/** Verified once at import: a changed file fails the suite before any browser work. */
for (const rel of [
	'(private path)',
	'(private path)',
	'(private path)',
	'tests/fixtures/phase7-dedup/other-silent-intro.mp3'
]) {
	verifiedFixtureBytes(rel);
}

test.setTimeout(120_000);

async function openHarness(page: Page): Promise<void> {
	await page.goto('/src/lib/rb/silence-watchdog.ts');
	const harnessReady = await page.evaluate(
		() => window.__silenceExpectedAudibleHarness !== undefined
	);
	if (!harnessReady) {
		await page.addScriptTag({ type: 'module', url: HARNESS_MODULE });
		await page.waitForFunction(() => window.__silenceExpectedAudibleHarness !== undefined);
	}
}

async function play(page: Page, scenario: Scenario): Promise<Outcome> {
	return page.evaluate(async (scenario) => {
		const harness = window.__silenceExpectedAudibleHarness!;
		return harness.runScenario(scenario);
	}, scenario);
}

/** The master really was silent while the watchdog watched, so the case tests the gate, not a loud bus. */
function expectSilentMaster(outcome: Outcome): void {
	expect(outcome.errors).toEqual([]);
	expect(outcome.samples).toBeGreaterThan(20);
	expect(
		outcome.held_rms_max,
		'the master tap was not silent while the watchdog sampled'
	).toBeLessThan(0.001);
}

function expectOpenAudible(outcome: Outcome): void {
	expect(outcome.errors, `harness errors: ${outcome.errors.join('; ')}`).toEqual([]);
	expect(
		outcome.open_rms,
		'the open graph never carried the track to the master tap'
	).toBeGreaterThan(0.01);
}

test.describe('the silence watchdog stops a deck only when sound was expected (real audio path)', () => {
	test.beforeEach(async ({ page }) => openHarness(page));

	test('if a channel fader at 0 (CUE on, pre-listening) stops the deck then broken', async ({
		page
	}) => {
		const o = await play(page, {
			path: 'mix',
			track: 'mixture',
			position_sec: 10,
			fader: 0,
			cue: true
		});
		expectOpenAudible(o);
		expectSilentMaster(o);
		expect(o.plans).toEqual([]);
		expect(o.deck_playing_after, 'a cued deck with its fader down keeps playing').toBe(true);
	});

	test('if muting every stem stops the deck then broken', async ({ page }) => {
		const o = await play(page, {
			path: 'stems',
			track: 'mixture',
			position_sec: 10,
			mute: ['vocal', 'instrumental']
		});
		expectOpenAudible(o);
		expectSilentMaster(o);
		expect(o.plans).toEqual([]);
	});

	test('if soloing a stem that is silent in this passage stops the deck then broken', async ({
		page
	}) => {
		// The real vocal stem is silent from 25.9 s to 29.9 s while the instrumental plays on.
		const o = await play(page, {
			path: 'stems',
			track: 'mixture',
			position_sec: 25.95,
			solo: 'vocal'
		});
		expectOpenAudible(o);
		expectSilentMaster(o);
		expect(o.plans).toEqual([]);
	});

	test("if a track's own silent passage stops the deck then broken", async ({ page }) => {
		// The fixture opens with 3 s of digital silence; the watchdog's 2 s window fits inside it.
		const o = await play(page, {
			path: 'mix',
			track: 'silentIntro',
			position_sec: 0,
			expectOpenSilent: true
		});
		expectSilentMaster(o);
		expect(o.plans).toEqual([]);
	});

	test('if a real dropout (signal expected, none delivered) does NOT stop the deck then broken', async ({
		page
	}) => {
		const o = await play(page, { path: 'mix', track: 'mixture', position_sec: 10, dropout: true });
		expectOpenAudible(o);
		expectSilentMaster(o);
		expect(o.plans.length, 'one dropout, one plan').toBe(1);
		expect(o.plans[0].stop_decks).toEqual([1]);
		expect(o.plans[0].toast).toMatch(/^AUDIO CUT decks=1 /);
		expect(o.deck_playing_after, 'the production dropout handler stopped the deck').toBe(false);
	});

	test('if soloing a LOUD stem hides a real dropout then broken', async ({ page }) => {
		// 5 s in, the real vocal stem is singing.
		const o = await play(page, {
			path: 'stems',
			track: 'mixture',
			position_sec: 5,
			solo: 'vocal',
			dropout: true
		});
		expectOpenAudible(o);
		expectSilentMaster(o);
		expect(o.plans.length).toBe(1);
		expect(o.plans[0].stop_decks).toEqual([1]);
		expect(o.deck_playing_after, 'the production dropout handler stopped the deck').toBe(false);
	});

	test('if the live-upgrade envelope scan holds the main thread for a whole part then broken', async ({
		page
	}) => {
		// P1 thread r4279728753: AlignedStemDeckProcessor.create scans every part
		// before the PCM moves to the worklet. On a real decoded stem the scan must
		// hand the main thread back between slices and still equal the one-pass scan.
		const r = await page.evaluate(async (stableId) => {
			const pcmUrl: string = '/src/lib/rb/silence-source-pcm.ts';
			const pcm = await import(/* @vite-ignore */ pcmUrl);
			const ctx = new AudioContext({ sampleRate: 44_100 });
			const res = await fetch(`/api/v1/tracks/${stableId}/stems/vocals`);
			if (!res.ok) throw new Error(`vocals stem HTTP ${res.status}`);
			const buffer = await ctx.decodeAudioData(await res.arrayBuffer());
			await ctx.close();
			let ticks = 0;
			const timer = setInterval(() => (ticks += 1), 0);
			const started = performance.now();
			const sliced = await pcm.peakEnvelopeYielding(buffer, pcm.PEAK_ENVELOPE_BLOCK_SEC, 1);
			const elapsed = performance.now() - started;
			clearInterval(timer);
			const whole = pcm.peakEnvelope(buffer);
			return {
				ticks,
				elapsed,
				blocks: whole.peaks.length,
				same:
					whole.block_sec === sliced.block_sec &&
					whole.duration_sec === sliced.duration_sec &&
					whole.peaks.every((v: number, i: number) => Object.is(v, sliced.peaks[i]))
			};
		}, MIXTURE_STABLE_ID);
		expect(r.blocks).toBe(600);
		expect(r.same, 'the sliced scan disagrees with the one-pass scan').toBe(true);
		expect(
			r.ticks,
			`no task ran during a ${r.elapsed.toFixed(1)} ms scan: it never yielded`
		).toBeGreaterThan(0);
	});
});
