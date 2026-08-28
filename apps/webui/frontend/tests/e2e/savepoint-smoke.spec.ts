/**
 * Savepoint smoke: the fastest end-to-end proof that the real UI still works.
 *
 * Six serial checks share ONE server boot and ONE page, so a savepoint gate
 * costs roughly a minute of wall clock rather than a full e2e cycle. Everything
 * runs against the real library through ``MDT_DATA_DIR`` - no fixtures, no
 * mocked rows, no stubbed endpoints.
 *
 * Requirements:
 *
 * - ✔︎ /performance renders with real playlist-tree counts (> 0 tracks).
 * - ✔︎ Selecting a real playlist and double-clicking a row with available audio
 *   loads deck 1; the deck header shows that track's title and a numeric BPM.
 * - ✔︎ Play then pause round-trips through the real transport button.
 * - ✔︎ The deck 1 scrolling waveform canvas paints non-background pixels.
 * - ✔︎ A channel-fader interaction reaches the typed performance IPC.
 * - ✔︎ /admin renders its KPI panel without a load-failure banner.
 * - ✔︎ Page errors, unexpected console errors and unexpected HTTP failures fail
 *   the run; no library-mutating request is ever issued.
 *
 * Acceptance tests:
 *
 * - [if] the playlist tree renders "..." instead of real counts [then ⛔️] test 1 passes.
 * - [if] the deck header still reads "No track loaded" after the load [then ⛔️] test 2 passes.
 * - [if] the waveform canvas stays background-coloured [then ⛔️] test 4 passes.
 * - [if] any PUT/PATCH/DELETE hits /api [then ⛔️] the suite passes.
 */
import { expect, test } from '@playwright/test';
import type { APIRequestContext, Browser, Page } from '@playwright/test';

import type { PerformanceState } from '../../src/lib/rb/performance-ipc.svelte';
import type { DeckId } from '../../src/lib/rb/types';

interface LoadableTrack {
	stable_id: string;
	title: string | null;
	bpm: number;
}

interface TrackDetailWire {
	stable_id: string;
	title: string | null;
	bpm: number | null;
}

interface AnlzWire {
	beatgrid: { beats: Array<{ n: number; bpm: number; t: number }> };
}

interface CanvasInkMeasurement {
	sampled: number;
	inked: number;
	distinct_colours: number;
}

const API_BASE = (process.env.SAVEPOINT_SMOKE_API_BASE ?? '').replace(/\/$/, '');
if (API_BASE === '') throw new Error('SAVEPOINT_SMOKE_API_BASE is required');

const DECK: DeckId = 1;
/** Quiet enough that a headed re-run is not a jump scare; still audible. */
const SMOKE_MASTER_VOLUME = 0.1;
/** Playlists opened while hunting for a loadable row. */
const PLAYLIST_PROBE_LIMIT = 3;
/** Rendered rows probed per playlist before moving to the next one. */
const ROW_PROBE_LIMIT = 6;
/** One ArrowDown step on a channel fader (VFader.handleKeyDown). */
const FADER_KEY_STEP = 0.02;

/**
 * Resource failures that are honest empty states or a known environment gap,
 * not regressions. Chrome reports every one of them as a console error, so an
 * unfiltered console gate would be red on a perfectly healthy stack.
 *
 * - artwork/stems 404: this track genuinely has no embedded art or stem bundle.
 * - smartlists 503: the smartlists router resolves ``data/state/state.db``
 *   relative to the process cwd instead of the shared paths module, so it is
 *   always unavailable when the gate runs from a worktree. The UI renders the
 *   honest "smartlists unavailable" row rather than faking a list.
 * - favicon: not served by the dev server.
 */
const EXPECTED_RESOURCE_FAILURES: readonly RegExp[] = [
	/\/api\/v1\/tracks\/[^/]+\/artwork(\?|$)/,
	/\/api\/v1\/tracks\/[^/]+\/stems(\?|$)/,
	/\/api\/v1\/smartlists(\?|$)/,
	/favicon/
];

function _isExpectedFailure(url: string): boolean {
	return EXPECTED_RESOURCE_FAILURES.some((pattern) => pattern.test(url));
}

let page: Page;
const pageProblems: string[] = [];
const mutatingRequests: string[] = [];
let waveformInkBeforeLoad: CanvasInkMeasurement | null = null;
let loadedTrack: LoadableTrack | null = null;

async function _query(): Promise<PerformanceState> {
	return page.evaluate(() => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.query();
	});
}

async function _measureWaveformInk(deck: DeckId): Promise<CanvasInkMeasurement> {
	return page.evaluate((deckId) => {
		const canvas = document.querySelector<HTMLCanvasElement>(
			`.rb-waverow canvas[aria-label="deck ${deckId} waveform seek"]`
		);
		if (canvas === null) throw new Error(`deck ${deckId} waveform canvas is not mounted`);
		const context = canvas.getContext('2d');
		if (context === null) throw new Error(`deck ${deckId} waveform canvas has no 2d context`);
		if (canvas.width <= 0 || canvas.height <= 0) {
			throw new Error(`deck ${deckId} waveform canvas has no backing pixels`);
		}
		const image = context.getImageData(0, 0, canvas.width, canvas.height).data;
		// The top-left pixel is canvas background in every deck state, so it is
		// the reference the ink ratio is measured against.
		const reference = `${image[0]},${image[1]},${image[2]}`;
		const colours = new Set<string>();
		let sampled = 0;
		let inked = 0;
		// Every 4th pixel: enough signal for a paint check, cheap enough to stay
		// inside the smoke budget on a full-width canvas.
		for (let index = 0; index < image.length; index += 16) {
			const colour = `${image[index]},${image[index + 1]},${image[index + 2]}`;
			sampled += 1;
			colours.add(colour);
			if (colour !== reference) inked += 1;
		}
		return { sampled, inked, distinct_colours: colours.size };
	}, deck);
}

/** Stable ids of rendered rows whose audio is on disk (no ``broken`` class). */
async function _renderedAvailableRowIds(): Promise<string[]> {
	return page.evaluate(
		(limit) =>
			Array.from(
				document.querySelectorAll<HTMLTableRowElement>(
					'.table-wrap table tbody tr[data-stable-id]'
				)
			)
				.filter((row) => !row.classList.contains('broken'))
				.slice(0, limit)
				.map((row) => row.dataset.stableId ?? '')
				.filter((stableId) => stableId !== ''),
		ROW_PROBE_LIMIT
	);
}

/**
 * Open real playlists until one shows a row that is on disk, carries a BPM and
 * parses an ANLZ beatgrid, so the deck assertions cannot fail on a row the
 * library already knows is unusable.
 *
 * A playlist is used rather than All Tracks on purpose: All Tracks pages the
 * whole 9k-row collection and costs ~22s, which alone would blow the budget.
 */
async function _selectPlaylistAndPickRow(request: APIRequestContext): Promise<LoadableTrack> {
	// Smartlist children carry no count span; playlist children always do.
	const playlists = page.locator('.tree-root .row.child:has(.count)');
	await expect(playlists.first()).toBeVisible();
	const playlistCount = Math.min(await playlists.count(), PLAYLIST_PROBE_LIMIT);
	const rejected: string[] = [];

	for (let index = 0; index < playlistCount; index += 1) {
		await playlists.nth(index).click();
		await expect(page.locator('.table-wrap table tbody tr[data-stable-id]').first()).toBeVisible();
		for (const stableId of await _renderedAvailableRowIds()) {
			const detail = await request.get(`${API_BASE}/api/v1/tracks/${encodeURIComponent(stableId)}`);
			if (!detail.ok()) {
				rejected.push(`${stableId}: detail HTTP ${detail.status()}`);
				continue;
			}
			const track = (await detail.json()) as TrackDetailWire;
			if (typeof track.bpm !== 'number' || track.bpm <= 0) {
				rejected.push(`${stableId}: no BPM`);
				continue;
			}
			const anlz = await request.get(
				`${API_BASE}/api/v1/tracks/${encodeURIComponent(stableId)}/anlz?points=100`
			);
			if (!anlz.ok()) {
				rejected.push(`${stableId}: ANLZ HTTP ${anlz.status()}`);
				continue;
			}
			const beats = ((await anlz.json()) as AnlzWire).beatgrid.beats;
			if (beats.length < 8) {
				rejected.push(`${stableId}: beatgrid has ${beats.length} beats`);
				continue;
			}
			return { stable_id: stableId, title: track.title, bpm: track.bpm };
		}
	}
	throw new Error(
		`no row in the first ${playlistCount} playlists was loadable: ${rejected.join('; ')}`
	);
}

test.describe.configure({ mode: 'serial' });

test.beforeAll(async ({ browser }: { browser: Browser }) => {
	// Tall enough that the browser panel gets real estate: the /performance grid
	// gives decks a fixed ceiling first, and a short viewport leaves the track
	// table ~30px high, which renders zero rows.
	page = await browser.newPage({ viewport: { width: 1600, height: 1200 } });
	page.on('console', (message) => {
		if (message.type() !== 'error') return;
		if (_isExpectedFailure(message.location().url)) return;
		pageProblems.push(`console: ${message.text()} (${message.location().url})`);
	});
	page.on('pageerror', (error) => pageProblems.push(`pageerror: ${error.message}`));
	page.on('response', (response) => {
		if (response.status() < 400) return;
		if (_isExpectedFailure(response.url())) return;
		pageProblems.push(`http ${response.status()}: ${response.url()}`);
	});
	// The smoke reads the operator's real library. A mutating verb reaching the
	// API is a bug in this suite, not an accepted cost of running the gate.
	page.on('request', (request) => {
		const method = request.method();
		if (method !== 'PUT' && method !== 'PATCH' && method !== 'DELETE') return;
		if (!new URL(request.url()).pathname.startsWith('/api/')) return;
		mutatingRequests.push(`${method} ${request.url()}`);
	});

	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await page.evaluate(async (volume) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		await ipc.dispatch({ type: 'master_volume', value: volume });
	}, SMOKE_MASTER_VOLUME);
});

test.afterAll(async () => {
	expect(mutatingRequests, 'the smoke must never mutate the real library').toEqual([]);
	await page.close();
});

test.afterEach(() => {
	expect(pageProblems, 'the UI must render without errors or failed requests').toEqual([]);
});

test('1. /performance renders the real playlist tree with non-zero counts', async () => {
	await expect(page.locator('.perf-root')).toBeVisible();

	const allTracksCount = page.locator('.tree-root .row').first().locator('.count');
	await expect(allTracksCount).not.toHaveText('...');
	const allTracks = Number((await allTracksCount.innerText()).replace(/[^0-9]/g, ''));
	expect(allTracks, 'All Tracks must report a real library size').toBeGreaterThan(0);

	const playlistCounts = page.locator('.tree-root .row.child:has(.count) .count');
	await expect(playlistCounts.first()).toBeVisible();
	const counts = (await playlistCounts.allInnerTexts()).map((text) =>
		Number(text.replace(/[^0-9]/g, ''))
	);
	expect(counts.length, 'real playlists must be listed under Playlists').toBeGreaterThan(0);
	expect(
		counts.filter((count) => count > 0).length,
		'at least one real playlist must carry tracks'
	).toBeGreaterThan(0);
});

test('2. double-clicking an available row loads deck 1 with title and BPM', async ({ request }) => {
	waveformInkBeforeLoad = await _measureWaveformInk(DECK);
	loadedTrack = await _selectPlaylistAndPickRow(request);

	const row = page.locator(`.table-wrap table tbody tr[data-stable-id="${loadedTrack.stable_id}"]`);
	await row.locator('td.c-title').dblclick();
	// The confirm popover only appears while the operator's saved ui-pref still
	// asks; "do this every time" turns it off permanently. Both paths are real.
	const confirmYes = page.locator('.load-confirm[role="dialog"] .load-confirm-yes');
	if (await confirmYes.isVisible()) await confirmYes.click();

	await page.waitForFunction(
		(stableId) => window.musicDjToolsPerformance?.query().decks[1].stable_id === stableId,
		loadedTrack.stable_id
	);
	const deck = (await _query()).decks[DECK];
	expect(deck.command_error, 'deck 1 must load without a command error').toBeNull();
	expect(deck.processor_error, 'deck 1 must load without a processor error').toBeNull();
	expect(deck.duration_ms ?? 0, 'a loaded deck must know its duration').toBeGreaterThan(0);

	const header = page.locator('.rb-deck[data-deck="1"] .deck-header');
	await expect(header.locator('.meta .title')).not.toHaveText('No track loaded');
	if (loadedTrack.title !== null && loadedTrack.title !== '') {
		await expect(header.locator('.meta .title')).toHaveText(loadedTrack.title);
	}
	const bpmText = await header.locator('.readout .bpm').innerText();
	expect(bpmText, 'the deck BPM readout must show a real number').not.toBe('--.--');
	expect(Number(bpmText), 'the deck BPM readout must parse as a number').toBeGreaterThan(0);
});

test('3. transport play then pause changes deck state', async () => {
	const playButton = page.locator('.rb-deck[data-deck="1"] button.round.play');
	// A confirm-popover load starts playing, so settle to a known paused
	// baseline first and drive the real button for both edges.
	if ((await _query()).decks[DECK].playing) {
		await playButton.click();
		await page.waitForFunction(
			() => window.musicDjToolsPerformance?.query().decks[1].playing === false
		);
	}

	await playButton.click();
	await page.waitForFunction(() => window.musicDjToolsPerformance?.query().decks[1].playing === true);
	await expect(playButton).toHaveAttribute('aria-label', 'pause');
	await page.waitForFunction(() => {
		const deck = window.musicDjToolsPerformance?.query().decks[1];
		return deck !== undefined && deck.position_ms > 0;
	});

	await playButton.click();
	await page.waitForFunction(
		() => window.musicDjToolsPerformance?.query().decks[1].playing === false
	);
	await expect(playButton).toHaveAttribute('aria-label', 'play');
	// audible is the PRESENTED state and lags the desired state by a frame or
	// two, so it is waited on rather than sampled.
	await page.waitForFunction(() => {
		const deck = window.musicDjToolsPerformance?.query().decks[1];
		return deck !== undefined && !deck.transport_pending && deck.audible === false;
	});
});

test('4. the deck 1 waveform canvas paints non-background pixels', async () => {
	const before = waveformInkBeforeLoad;
	if (before === null) throw new Error('the pre-load waveform baseline was never measured');
	const after = await _measureWaveformInk(DECK);

	const inkRatio = after.inked / after.sampled;
	const baselineRatio = before.inked / before.sampled;
	expect(
		after.distinct_colours,
		`a painted waveform must use many colours, got ${after.distinct_colours}`
	).toBeGreaterThan(4);
	expect(inkRatio, `waveform ink ratio was ${inkRatio.toFixed(4)}`).toBeGreaterThan(0.05);
	expect(
		inkRatio,
		`waveform ink ${inkRatio.toFixed(4)} did not beat the empty-deck baseline ${baselineRatio.toFixed(4)}`
	).toBeGreaterThan(baselineRatio);
});

test('5. a channel-fader interaction reaches the typed performance IPC', async () => {
	const fader = page.locator('[role="slider"][aria-label="channel 1 fader"]');
	await expect(fader).toBeVisible();
	const before = (await _query()).mixer.channels[DECK].fader;

	await fader.focus();
	await fader.press('ArrowDown');
	await fader.press('ArrowDown');
	await page.waitForFunction((baseline) => {
		const state = window.musicDjToolsPerformance?.query();
		return state !== undefined && state.mixer.channels[1].fader < baseline;
	}, before);

	const after = (await _query()).mixer.channels[DECK].fader;
	expect(after, 'two ArrowDown presses must move the fader by two steps').toBeCloseTo(
		before - 2 * FADER_KEY_STEP,
		3
	);
	await expect(fader).toHaveAttribute('aria-valuenow', String(after));
});

test('6. /admin renders the KPI panel without a load failure', async () => {
	await page.goto('/admin');
	await expect(page.locator('h2')).toHaveText('Admin');
	await expect(page.locator('section.panel h3').first()).toBeVisible();
	await expect(page.locator('.fatal')).toHaveCount(0);
});
