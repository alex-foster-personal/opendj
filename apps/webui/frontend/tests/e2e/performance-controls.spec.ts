import { expect, test } from '@playwright/test';
import type { APIRequestContext, Page } from '@playwright/test';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { beatgridFor } from './support/analyzed-track';

const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const DATA_DIR = process.env.MDT_DATA_DIR ?? join(REPOSITORY_ROOT, 'data');
const IS_GENERATED_FIXTURE = existsSync(join(DATA_DIR, 'fixture-revision.txt'));

function _skipOrFailSmoke(condition: boolean, reason: string): void {
	if (!condition) return;
	if (process.env.PERFORMANCE_E2E_SMOKE === '1') {
		throw new Error(`performance-smoke fail-fast: ${reason}`);
	}
	test.skip(true, reason);
}

import type {
	PerformanceCommand,
	PerformanceDeckSnapshot as PerformanceDeckState,
	PerformanceState
} from '../../src/lib/rb/performance-ipc.svelte';
import type { DeckId } from '../../src/lib/rb/deck-slots';

interface TrackWire {
	stable_id: string;
	bpm: number | null;
	file_exists: boolean;
	title: string | null;
}

interface BeatWire {
	n: number;
	bpm: number;
	t: number;
}

interface AnlzWire {
	beatgrid: { beats: BeatWire[] };
}

interface RealTrack {
	stable_id: string;
	title: string;
	bpm: number;
	beats: BeatWire[];
	beatgrid_ms: number[];
}

interface ToneMeasurement {
	dominant_hz: number;
	input_rate: number;
}

type StretchAdapterModule = {
	StretchDeckProcessor: {
		create(
			context: AudioContext,
			options: {
				onInputTime: (inputTimeSec: number) => void;
				onProcessorError: (error: Error) => void;
			}
		): Promise<{
			connect(destination: AudioNode): void;
			disconnect(): void;
			dispose(): Promise<void>;
			latencySec(): Promise<number>;
			load(buffer: AudioBuffer): Promise<void>;
			schedule(
				outputTime: number,
				change: { active: boolean; input: number; rate: number; semitones: number }
			): Promise<void>;
			stop(outputTime: number): Promise<void>;
		}>;
	};
};

interface BeatGridPixelMeasurement {
	full_height_downbeats: number;
	ordinary_bottom_matches: number;
	downbeat_bottom_luma: number;
	ordinary_bottom_luma: number;
}

const API_BASE = process.env.PERFORMANCE_E2E_API_BASE ?? 'http://127.0.0.1:8686';
const UI_BASE = (
	process.env.PERFORMANCE_E2E_BASE_URL ??
	process.env.PERFORMANCE_E2E_UI_BASE ??
	''
).replace(/\/$/, '');
const DECK_IDS: readonly DeckId[] = [1, 2, 3, 4];
const SYNC_MASTER_RATE = 1.1;

/**
 * LATENCY-01/03 behavioural floor. Asserted against the number the ENGINE
 * LOGGED (`scheduled_offset_ms`, emitted post-clamp inside _scheduleDeckSerial
 * and read back through `window.__mdtPerfLog()`), never a wall clock measured
 * from Playwright - a wall clock flakes under CI load and would be ratcheted
 * down to whatever the quietest runner managed.
 *
 * Two ceilings, because they ratchet independently:
 *
 * TRANSPORT_SAFETY_CEILING_MS is the part the schedule POLICY owns, i.e. the
 * offset with the processor's LEAD removed. It is the 100ms beat-sync margin
 * that used to be charged to a plain play/pause, and it is now 8ms. This is the
 * number round 1 moved, and it is machine-independent. Round 2 changed what is
 * subtracted (the onset-ramp lead, not the self-report) precisely so this term
 * keeps meaning what its name says.
 *
 * TRANSPORT_OFFSET_CEILING_MS is the whole scheduled offset, and it RATCHETS:
 * measured at exactly 220.0ms on 48/48 samples before round 1, ~128ms after it,
 * ~52.4ms after round 2 replaced the processor self-report with the measured
 * onset ramp. 60ms is that 52.4ms plus room for the clamp, and it is now tight
 * enough that reinstating the self-report as the lead (128ms) fails the gate.
 *
 * It is still deliberately NOT set at the 30ms LATENCY-01 budget. The remaining
 * ~44ms is the onset ramp of the shipped 120ms STFT block, and the only way to
 * shorten it is to shorten the block - which is step 2, gated on a quality
 * methodology this commit does not run. Setting this at 30ms today would red
 * the gate on a defect this commit does not claim to have fixed.
 */
const TRANSPORT_SAFETY_CEILING_MS = 30;
const TRANSPORT_OFFSET_CEILING_MS = 60;

interface PerfEventRow {
	kind: string;
	deck: number | null;
	stages?: Record<string, number>;
}

let analyzedTracks: RealTrack[] | null = null;
let anlzDiscoveryReport: { selected: string[]; errors: string[] } | null = null;

async function _waitForIpc(page: Page): Promise<void> {
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
}

/** Quiet enough for headed laptop runs; still >0 so audible/presentation checks work. */
const E2E_MASTER_VOLUME = 0.1;

async function _gotoPerformance(page: Page): Promise<void> {
	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto(`${UI_BASE}/performance`);
	await _waitForIpc(page);
	await _dispatch(page, { type: 'master_volume', value: E2E_MASTER_VOLUME });
}

/** Rows the engine logged for one deck's transport schedules, newest last. */
async function _scheduleOffsetRows(page: Page, deck: DeckId): Promise<PerfEventRow[]> {
	return page.evaluate((deckId) => {
		const read = (window as unknown as { __mdtPerfLog?: () => readonly PerfEventRow[] })
			.__mdtPerfLog;
		if (read === undefined) {
			throw new Error('__mdtPerfLog is not installed; the latency instrument is missing');
		}
		return read().filter(
			(row) =>
				row.kind === 'transport-schedule' &&
				row.deck === deckId &&
				row.stages !== undefined &&
				typeof row.stages.scheduled_offset_ms === 'number'
		) as PerfEventRow[];
	}, deck);
}

async function _query(page: Page): Promise<PerformanceState> {
	await _waitForIpc(page);
	return page.evaluate(() => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.query();
	});
}

async function _dispatch(page: Page, command: PerformanceCommand): Promise<PerformanceState> {
	await _waitForIpc(page);
	return page.evaluate(async (message) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.dispatch(message);
	}, command);
}

async function _idleState(page: Page): Promise<PerformanceState> {
	await _waitForIpc(page);
	await page.waitForFunction(() => window.musicDjToolsPerformance?.query().command_pending === false);
	return _query(page);
}

async function _firstPresentedTransportState(
	page: Page,
	deck: DeckId,
	audible: boolean
): Promise<PerformanceState> {
	await _waitForIpc(page);
	return page.evaluate(
		async ({ deckId, expectedAudible }) => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) throw new Error('performance IPC is not installed');
			const deadline = performance.now() + 15_000;
			let lastDeck = ipc.query().decks[deckId];
			while (performance.now() < deadline) {
				const state = ipc.query();
				const candidate = state.decks[deckId];
				lastDeck = candidate;
				if (
					candidate.audible === expectedAudible &&
					!candidate.transport_pending &&
					candidate.transport_clock.presented_revision ===
						candidate.transport_clock.desired_revision
				) {
					return state;
				}
				await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
			}
			throw new Error(
				`deck ${deckId} did not reach presented audible=${String(expectedAudible)} state; ` +
					`last deck state=${JSON.stringify(lastDeck)}`
			);
		},
		{ deckId: deck, expectedAudible: audible }
	);
}

async function _nextTransportPendingState(
	page: Page,
	deck: DeckId,
	afterDesiredRevision: number
): Promise<PerformanceState> {
	const handle = await page.waitForFunction(
		({ deckId, baselineRevision }) => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) throw new Error('performance IPC is not installed');
			const state = ipc.query();
			const candidate = state.decks[deckId];
			return candidate.transport_pending &&
				candidate.transport_clock.desired_revision > baselineRevision
				? state
				: false;
		},
		{ deckId: deck, baselineRevision: afterDesiredRevision }
	);
	try {
		return (await handle.jsonValue()) as PerformanceState;
	} finally {
		await handle.dispose();
	}
}

async function _waveformIpcPositionDeltaMs(page: Page, deck: DeckId): Promise<number> {
	return page.evaluate((deckId) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		const waveform = document.querySelector(
			`.rb-waverow canvas[aria-label="deck ${deckId} waveform seek"]`
		);
		if (!(waveform instanceof HTMLCanvasElement)) {
			throw new Error(`deck ${deckId} scrolling waveform is not mounted`);
		}
		const rendered = Number(waveform.getAttribute('aria-valuenow'));
		if (!Number.isFinite(rendered)) throw new Error(`deck ${deckId} waveform aria value is invalid`);
		return Math.abs(rendered - ipc.query().decks[deckId].position_ms);
	}, deck);
}

async function _fetchAnalyzedTracks(request: APIRequestContext): Promise<RealTrack[]> {
	if (analyzedTracks !== null) return analyzedTracks;
	const tracksResponse = await request.get(`${API_BASE}/api/v1/tracks?limit=1000&available=true`);
	expect(tracksResponse.ok(), 'real available-track listing must succeed').toBeTruthy();
	const payload = (await tracksResponse.json()) as { items: TrackWire[] };
	const candidates = payload.items.filter(
		(track): track is TrackWire & { bpm: number } =>
			track.file_exists && typeof track.bpm === 'number' && track.bpm > 0
	);
	_skipOrFailSmoke(
		candidates.length <= 1,
		'requires a real library with at least two BPM-tagged, on-disk tracks'
	);

	const found: RealTrack[] = [];
	const discoveryErrors: string[] = [];
	// The backend ANLZ parser is CPU-bound and some real library entries are
	// corrupt. Discover sequentially so one recorded 500 cannot reset an
	// unrelated valid track's ranged audio response.
	for (const track of candidates) {
		if (found.length >= 8) break;
		// TWO real sources, in the product's own documented order. A
		// rekordbox-mapped track's authoritative grid is on /anlz; a locally
		// ingested one has an empty /anlz beatgrid by design and its measured
		// apps.analysis grid on /beatgrid-fallback, in the identical shape
		// (see routes/analysis.py). Reading only one of them silently
		// excludes the whole other kind of library: /beatgrid-fallback alone
		// can 404 for rekordbox-mapped rows, and /anlz alone
		// returns an empty grid for every row of the generated fixture until
		// the rescue builder has run librosa analysis.
		const anlz = await beatgridFor(request, API_BASE, track.stable_id, discoveryErrors);
		if (anlz === null) continue;
		const beatgrid_ms = anlz.beatgrid.beats.map((beat) => beat.t * 1000);
		if (beatgrid_ms.length < 32) continue;
		const audio = await request.get(
			`${API_BASE}/api/v1/tracks/${encodeURIComponent(track.stable_id)}/audio`,
			{ headers: { Range: 'bytes=0-4095' } }
		);
		expect(
			[200, 206],
			`real audio stream for ${track.stable_id} must return 200 or 206`
		).toContain(audio.status());
		expect(
			(await audio.body()).byteLength,
			`real audio stream for ${track.stable_id} is empty`
		).toBeGreaterThan(0);
		found.push({
			stable_id: track.stable_id,
			title: track.title ?? track.stable_id,
			bpm: track.bpm,
			beats: anlz.beatgrid.beats,
			beatgrid_ms
		});
	}
	anlzDiscoveryReport = {
		selected: found.map((track) => track.stable_id),
		errors: discoveryErrors
	};
	// Nothing at all, and nothing errored: this library simply carries no
	// beatgrids - an unmapped local library whose `analysis` extra is not
	// installed, which is an ordinary correct checkout. That is the same
	// class of "this environment cannot exercise this" as the two-track skip
	// above, so it skips with a named reason. It is deliberately NOT widened
	// to cover `found.length === 1` or a non-empty error list: one grid where
	// two are needed, or a grid that failed to parse, is a real defect and
	// still fails here.
	_skipOrFailSmoke(
		found.length === 0 && discoveryErrors.length === 0,
		'no available track exposes a beatgrid on /anlz (rekordbox) or /beatgrid-fallback ' +
			'(apps.analysis); point MDT_DATA_DIR at an analyzed library'
	);
	expect(
		found.length,
		`real library must expose at least two playable tracks with beatgrids; discovery errors: ${discoveryErrors.join(', ')}`
	).toBeGreaterThan(1);
	analyzedTracks = found;
	return found;
}

async function _realSyncPair(request: APIRequestContext): Promise<[RealTrack, RealTrack]> {
	const tracks = await _fetchAnalyzedTracks(request);
	for (const master of tracks) {
		for (const follower of tracks) {
			if (master.stable_id === follower.stable_id) continue;
			const ratio = master.bpm / follower.bpm;
			const changedMasterRatio = ratio * SYNC_MASTER_RATE;
			const materiallyDifferent = Math.abs(ratio - 1) >= 0.005;
			const initialRatioFits = ratio >= 0.84 && ratio <= 1.16;
			const changedRatioFits = changedMasterRatio >= 0.84 && changedMasterRatio <= 1.16;
			if (materiallyDifferent && initialRatioFits && changedRatioFits) {
				return [master, follower];
			}
		}
	}
	throw new Error(
		`real library has no distinct-BPM sync pair within +-16% before and after ${SYNC_MASTER_RATE}x master rate`
	);
}

async function _barFoldSyncPair(request: APIRequestContext): Promise<[RealTrack, RealTrack]> {
	const tracks = await _fetchAnalyzedTracks(request);
	const master = tracks.find((track) => track.title === 'webkit-fixture-a-128bpm');
	const follower = tracks.find((track) => track.title === 'webkit-fixture-d-64bpm-fold');
	if (master === undefined || follower === undefined || master.stable_id === follower.stable_id) {
		throw new Error(
			'library has no webkit-fixture-a-128bpm / webkit-fixture-d-64bpm-fold pair with measured beatgrids'
		);
	}
	for (const track of [master, follower]) {
		const beatOnes = track.beats.filter((beat) => beat.n === 1);
		expect(beatOnes.length, `${track.stable_id} must publish real beat-1 markers`).toBeGreaterThan(0);
		expect(track.beatgrid_ms.length, `${track.stable_id} beatgrid`).toBeGreaterThan(31);
	}
	return [master, follower];
}

function _control(page: Page, deck: DeckId, control: string) {
	return page.locator(
		`section.rb-deck[data-deck="${deck}"] button[data-performance-control="${control}"]`
	);
}

function _timeToNextBeatMs(deck: PerformanceDeckState): number {
	const next = deck.beatgrid_ms.find((beat) => beat > deck.position_ms + 0.5);
	if (next === undefined) throw new Error(`deck ${deck.deck_id} has no next beat at ${deck.position_ms}`);
	return (next - deck.position_ms) / deck.pitch;
}

/** Mirrors beat-sync-math.isTempoLockedToMaster for folded BAR locks in e2e:
 * 0.1 BPM plus the phase lock's largest trim (0.3%) of the folded master, so
 * an ordinary trim does not flake the poll. */
function _tempoLockedToMaster(candidateBpm: number | null, masterBpm: number | null): boolean {
	if (candidateBpm === null || masterBpm === null) return false;
	if (!Number.isFinite(candidateBpm) || candidateBpm <= 0) return false;
	if (!Number.isFinite(masterBpm) || masterBpm <= 0) return false;
	const normalizations = [1, 0.5, 2];
	return normalizations.some((normalization) => {
		const folded = masterBpm * normalization;
		return Math.abs(candidateBpm - folded) <= 0.1 + 0.003 * folded;
	});
}

function _nearestBeatIndex(deck: PerformanceDeckState): number {
	if (deck.beatgrid_ms.length === 0) throw new Error(`deck ${deck.deck_id} has no beat grid`);
	let nearest = 0;
	let distance = Math.abs(deck.beatgrid_ms[0] - deck.position_ms);
	for (let index = 1; index < deck.beatgrid_ms.length; index++) {
		const candidateDistance = Math.abs(deck.beatgrid_ms[index] - deck.position_ms);
		if (candidateDistance < distance) {
			nearest = index;
			distance = candidateDistance;
		}
	}
	return nearest;
}

function _beatCoordinate(track: RealTrack, positionMs: number): { n: number; phase: number } {
	let beatIndex = -1;
	for (let index = 0; index < track.beats.length; index++) {
		if (track.beats[index].t * 1000 > positionMs) break;
		beatIndex = index;
	}
	if (beatIndex < 0 || beatIndex + 1 >= track.beats.length) {
		throw new RangeError(`${track.stable_id} has no enclosing PQTZ interval at ${positionMs}ms`);
	}
	const beat = track.beats[beatIndex];
	const next = track.beats[beatIndex + 1];
	const spanMs = (next.t - beat.t) * 1000;
	if (spanMs <= 0) throw new RangeError(`PQTZ interval ${beatIndex} is not strictly increasing`);
	return { n: beat.n, phase: (positionMs - beat.t * 1000) / spanMs };
}

function _normalizeLoopPositionMs(positionMs: number, loopInMs: number, loopOutMs: number): number {
	const spanMs = loopOutMs - loopInMs;
	if (spanMs <= 0) throw new RangeError(`loop span must be positive, got ${spanMs}`);
	return loopInMs + (((positionMs - loopInMs) % spanMs) + spanMs) % spanMs;
}

function _cyclicLoopDistanceMs(
	actualMs: number,
	expectedMs: number,
	loopInMs: number,
	loopOutMs: number
): number {
	const spanMs = loopOutMs - loopInMs;
	const actual = _normalizeLoopPositionMs(actualMs, loopInMs, loopOutMs);
	const expected = _normalizeLoopPositionMs(expectedMs, loopInMs, loopOutMs);
	const directDistance = Math.abs(actual - expected);
	return Math.min(directDistance, spanMs - directDistance);
}

function _centsBetween(actualHz: number, expectedHz: number): number {
	return 1200 * Math.log2(actualHz / expectedHz);
}

async function _measureBeatGridCanvas(
	page: Page,
	track: RealTrack,
	positionMs: number
): Promise<BeatGridPixelMeasurement> {
	return page.evaluate(
		({ beats, positionMs }) => {
			const foundCanvas = document.querySelector<HTMLCanvasElement>('.rb-waverow canvas');
			if (foundCanvas === null) throw new Error('deck 1 waveform canvas not found');
			const foundContext = foundCanvas.getContext('2d');
			if (foundContext === null) throw new Error('deck 1 waveform canvas has no 2d context');
			const canvas: HTMLCanvasElement = foundCanvas;
			const context: CanvasRenderingContext2D = foundContext;
			const rect = canvas.getBoundingClientRect();
			if (rect.width <= 0 || rect.height <= 0) throw new Error('deck 1 waveform canvas is hidden');

			function parseHex(name: string, raw: string): [number, number, number] {
				const match = /^#([0-9a-f]{6})$/i.exec(raw.trim());
				if (match === null) throw new Error(`${name} must be a six-digit hex colour, got ${raw}`);
				return [
					Number.parseInt(match[1].slice(0, 2), 16),
					Number.parseInt(match[1].slice(2, 4), 16),
					Number.parseInt(match[1].slice(4, 6), 16)
				];
			}

			const style = getComputedStyle(canvas);
			const background = parseHex('--rb-bg', style.getPropertyValue('--rb-bg'));
			const tick = parseHex('--rb-text', style.getPropertyValue('--rb-text'));
			const scaleX = canvas.width / rect.width;
			const scaleY = canvas.height / rect.height;
			const tLeftSec = positionMs / 1000 - 12;
			const pxPerSec = rect.width / 24;
			const playheadX = rect.width / 2;

			function pixel(cssX: number, cssY: number): [number, number, number] {
				const x = Math.max(0, Math.min(canvas.width - 1, Math.floor(cssX * scaleX)));
				const y = Math.max(0, Math.min(canvas.height - 1, Math.floor(cssY * scaleY)));
				const rgba = context.getImageData(x, y, 1, 1).data;
				return [rgba[0], rgba[1], rgba[2]];
			}

			function blend(alpha: number): [number, number, number] {
				return background.map((channel, index) =>
					Math.round(channel * (1 - alpha) + tick[index] * alpha)
				) as [number, number, number];
			}

			function distance(left: [number, number, number], right: [number, number, number]): number {
				return Math.max(...left.map((channel, index) => Math.abs(channel - right[index])));
			}

			function luma(rgb: [number, number, number]): number {
				return rgb[0] * 0.2126 + rgb[1] * 0.7152 + rgb[2] * 0.0722;
			}

			const samples = beats
				.map((beat) => {
					const x = Math.round((beat.t - tLeftSec) * pxPerSec);
					const sampleX = x + (beat.n === 1 ? 1 : 0.5);
					return {
						n: beat.n,
						x,
						top: pixel(sampleX, 7.5),
						bottom: pixel(sampleX, rect.height - 0.5)
					};
				})
				.filter(
					(sample) => sample.x >= 4 && sample.x < rect.width - 4 && Math.abs(sample.x - playheadX) > 5
				);
			const downbeatBottom = blend(0.55);
			const ordinaryBottom = blend(0.38);
			const fullHeightDownbeats = samples.filter(
				(sample) =>
					sample.n === 1 &&
					distance(sample.top, tick) <= 3 &&
					distance(sample.bottom, downbeatBottom) <= 3
			);
			const ordinaryBottomMatches = samples.filter(
				(sample) => sample.n !== 1 && distance(sample.bottom, ordinaryBottom) <= 3
			);
			return {
				full_height_downbeats: fullHeightDownbeats.length,
				ordinary_bottom_matches: ordinaryBottomMatches.length,
				downbeat_bottom_luma:
					fullHeightDownbeats.reduce((sum, sample) => sum + luma(sample.bottom), 0) /
					Math.max(1, fullHeightDownbeats.length),
				ordinary_bottom_luma:
					ordinaryBottomMatches.reduce((sum, sample) => sum + luma(sample.bottom), 0) /
					Math.max(1, ordinaryBottomMatches.length)
			};
		},
		{ beats: track.beats, positionMs }
	);
}

/** Deck-owned waveform surfaces, per DECKUX-04. Selectors, not components, so
 * this fails loudly if a surface is renamed rather than silently checking one.
 * The library browser's PreviewStrip is excluded on purpose: it is a listing
 * surface under a draw-once policy (SPIKE-A2), not a deck surface. */
const DECK1_WAVE_SURFACES: ReadonlyArray<{ name: string; selector: string }> = [
	{ name: 'wavestack row', selector: '.rb-waverow canvas' },
	{ name: 'deck overview strip', selector: 'section.rb-deck[data-deck="1"] .strip canvas' }
];

/**
 * Record each surface's current pixels in the page, keyed by selector.
 *
 * Kept browser-side on purpose: the comparison is over every pixel of every
 * surface, and shipping those bytes to Node per sample would dominate runtime.
 */
async function _recordSurfaceBaseline(page: Page): Promise<void> {
	await page.evaluate((surfaces) => {
		const store: Record<string, Uint8ClampedArray> = {};
		for (const surface of surfaces) {
			const canvas = document.querySelector<HTMLCanvasElement>(surface.selector);
			if (canvas === null) throw new Error(`${surface.name}: no canvas at ${surface.selector}`);
			const context = canvas.getContext('2d');
			if (context === null) throw new Error(`${surface.name}: canvas has no 2d context`);
			if (canvas.width === 0 || canvas.height === 0) {
				throw new Error(`${surface.name}: canvas is zero-sized, nothing could ever paint`);
			}
			store[surface.selector] = context
				.getImageData(0, 0, canvas.width, canvas.height)
				.data.slice();
		}
		(window as unknown as { __loopBaseline?: Record<string, Uint8ClampedArray> }).__loopBaseline =
			store;
	}, DECK1_WAVE_SURFACES);
}

/**
 * Pixels that changed on each surface since the baseline, plus the total
 * sampled, so a zero always comes with the denominator that makes it readable.
 *
 * Counts REPAINTED pixels rather than orange ones deliberately: the strip's own
 * low band is #e8a13a, byte-identical to the loop band's base color, so a color
 * census cannot tell the loop from the waveform underneath it. A before/after
 * delta can.
 */
async function _surfaceRepaintCounts(
	page: Page
): Promise<Array<{ name: string; changed: number; sampled: number }>> {
	return page.evaluate((surfaces) => {
		const store = (window as unknown as { __loopBaseline?: Record<string, Uint8ClampedArray> })
			.__loopBaseline;
		if (store === undefined) throw new Error('no baseline recorded; call the baseline helper first');
		return surfaces.map((surface) => {
			const canvas = document.querySelector<HTMLCanvasElement>(surface.selector);
			if (canvas === null) throw new Error(`${surface.name}: no canvas at ${surface.selector}`);
			const context = canvas.getContext('2d');
			if (context === null) throw new Error(`${surface.name}: canvas has no 2d context`);
			const before = store[surface.selector];
			const after = context.getImageData(0, 0, canvas.width, canvas.height).data;
			if (before === undefined) throw new Error(`${surface.name}: no baseline for this surface`);
			if (before.length !== after.length) {
				throw new Error(`${surface.name}: canvas resized between samples, delta is meaningless`);
			}
			let changed = 0;
			for (let i = 0; i < after.length; i += 4) {
				if (
					before[i] !== after[i] ||
					before[i + 1] !== after[i + 1] ||
					before[i + 2] !== after[i + 2] ||
					before[i + 3] !== after[i + 3]
				) {
					changed++;
				}
			}
			return { name: surface.name, changed, sampled: after.length / 4 };
		});
	}, DECK1_WAVE_SURFACES);
}

test.describe('performance controls and browser IPC', () => {
	test('Mixtour Bounce release cannot exit a loop queued after reload', async ({ page, request }) => {
		test.setTimeout(240_000);
		const stableId = process.env.PERFORMANCE_E2E_MIXTOUR_TRACK ?? (await _fetchAnalyzedTracks(request))[0].stable_id;
		const grid = (await beatgridFor(request, API_BASE, stableId, []))?.beatgrid.beats.map((beat) => beat.t * 1000) ?? [];
		expect(grid.length).toBeGreaterThan(32);
		await _gotoPerformance(page);
		await _dispatch(page, { type: 'load', deck: 1, stable_id: stableId });
		await _dispatch(page, { type: 'seek', deck: 1, position_ms: grid[8] });
		const controller = await page.evaluateHandle(async (url) => {
			const glue = await import(url) as typeof import('../../src/lib/rb/midi/action-glue.svelte');
			return glue.handleMidiAction;
		}, '/src/lib/rb/midi/action-glue.svelte.ts');
		await controller.evaluate((send) => {
			send({ type: 'controller_pad_mode', deck: 1, mode: 'bounce_loop' },
				{ kind: 'button', pressed: true, velocity: 127 }, 'mixtour-queued-release');
			send({ type: 'controller_pad', deck: 1, pad: 1, shifted: false },
				{ kind: 'button', pressed: true, velocity: 127 }, 'mixtour-queued-release');
		});
		expect((await _idleState(page)).decks[1].loop?.beat_length).toBe(0.03125);
		await controller.evaluate(async (send, { stableId, position }) => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) throw new Error('performance IPC is not installed');
			// Same JS turn: release observes the old deck, but its command waits
			// behind a genuine reload, seek and new loop in the shared scheduler.
			const reload = ipc.dispatch({ type: 'load', deck: 1, stable_id: stableId });
			const seek = ipc.dispatch({ type: 'seek', deck: 1, position_ms: position });
			const loop = ipc.dispatch({ type: 'beat_loop', deck: 1, beats: 4 });
			send({ type: 'controller_pad', deck: 1, pad: 1, shifted: false },
				{ kind: 'button', pressed: false, velocity: 0 }, 'mixtour-queued-release');
			await Promise.all([reload, seek, loop]);
		}, { stableId, position: grid[8] });
		expect((await _idleState(page)).decks[1].loop?.beat_length,
			'an old release must not cancel a new-track loop after waiting in the queue').toBe(4);
		await controller.evaluate(async (send, { stableId, position }) => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) throw new Error('performance IPC is not installed');
			const reload = ipc.dispatch({ type: 'load', deck: 1, stable_id: stableId });
			const seek = ipc.dispatch({ type: 'seek', deck: 1, position_ms: position });
			const loop = ipc.dispatch({ type: 'beat_loop', deck: 1, beats: 4 });
			// Both edges can arrive before the load starts. Neither may target
			// the replacement track just because the same deck number is used.
			for (const pressed of [true, false]) send(
				{ type: 'controller_pad', deck: 1, pad: 1, shifted: false },
				{ kind: 'button', pressed, velocity: pressed ? 127 : 0 }, 'mixtour-queued-release');
			await Promise.all([reload, seek, loop]);
		}, { stableId, position: grid[8] });
		expect((await _idleState(page)).decks[1].loop?.beat_length,
			'queued old-track press and release must both be inert').toBe(4);
		for (const generation of [0, -1, 1.5, '1']) {
			await expect(page.evaluate(async (generation) => {
				const ipc = window.musicDjToolsPerformance;
				if (ipc === undefined) throw new Error('performance IPC is not installed');
				await ipc.dispatch({ type: 'loop', deck: 1, loop: null, if_load_generation: generation });
			}, generation)).rejects.toThrow('generation must be a positive safe integer');
		}
		expect((await _idleState(page)).decks[1].loop?.beat_length).toBe(4);
		await _dispatch(page, { type: 'unload', deck: 1 });
		await controller.dispose();
	});

	test('Mixtour Bounce holds release safely across mode changes and reloads', async ({ page, request }) => {
		test.setTimeout(360_000);
		const stableId = process.env.PERFORMANCE_E2E_MIXTOUR_TRACK ?? (await _fetchAnalyzedTracks(request))[0].stable_id;
		const grid = (await beatgridFor(request, API_BASE, stableId, []))?.beatgrid.beats.map((beat) => beat.t * 1000) ?? [];
		expect(grid.length).toBeGreaterThan(32);
		await _gotoPerformance(page);
		const controller = await page.evaluateHandle(async (url) => {
			const glue = await import(url) as typeof import('../../src/lib/rb/midi/action-glue.svelte');
			return glue.handleMidiAction;
		}, '/src/lib/rb/midi/action-glue.svelte.ts');
		for (const deck of DECK_IDS) {
			const mode = async (mode: 'bounce_loop' | 'auto_loop' | 'pitch_cue') => {
				await controller.evaluate((send, args) => send(
					{ type: 'controller_pad_mode', deck: args.deck, mode: args.mode },
					{ kind: 'button', pressed: true, velocity: 127 }, 'mixtour-hold-proof'
				), { deck, mode });
				return _idleState(page);
			};
			const pad = async (pad: 1 | 2 | 5, pressed: boolean) => {
				await controller.evaluate((send, args) => send(
					{ type: 'controller_pad', deck: args.deck, pad: args.pad, shifted: false },
					{ kind: 'button', pressed: args.pressed, velocity: args.pressed ? 127 : 0 }, 'mixtour-hold-proof'
				), { deck, pad, pressed });
				return _idleState(page);
			};
			await _dispatch(page, { type: 'load', deck, stable_id: stableId });
			await _dispatch(page, { type: 'seek', deck, position_ms: grid[8] });
			await mode('bounce_loop');
			await pad(1, true);
			await pad(2, true);
			let state = await pad(1, false);
			expect(state.decks[deck].loop?.beat_length, 'older release must not cancel latest hold').toBe(0.0625);
			state = await pad(2, false);
			expect(state.decks[deck].loop).toBeNull();
			await pad(1, true);
			state = await mode('pitch_cue');
			expect(state.decks[deck].loop, 'mode change must release a held Bounce loop').toBeNull();
			await mode('auto_loop');
			state = await pad(5, true);
			expect(state.decks[deck].loop?.beat_length, 'Auto Loop must engage before testing late release').toBe(4);
			state = await pad(1, false);
			expect(state.decks[deck].loop?.beat_length, 'late release must not cancel a new Auto Loop').toBe(4);
			await pad(5, true);
			await mode('bounce_loop');
			await pad(1, true);
			await _dispatch(page, { type: 'load', deck, stable_id: stableId });
			await _dispatch(page, { type: 'seek', deck, position_ms: grid[8] });
			await _dispatch(page, { type: 'beat_loop', deck, beats: 4 });
			state = await pad(1, false);
			expect(state.decks[deck].loop?.beat_length, 'old hold must not mutate a reloaded track').toBe(4);
			await _dispatch(page, { type: 'loop', deck, loop: null });
			await pad(1, true);
			// Exercise the production disconnect cleanup without fabricating a
			// connected port. Physical removal/reconnect remains hardware QA.
			await page.evaluate(async (url) => {
				const glue = await import(url) as typeof import('../../src/lib/rb/midi/action-glue.svelte');
				glue.releaseControllerDevice('mixtour-hold-proof');
			}, '/src/lib/rb/midi/action-glue.svelte.ts');
			state = await _idleState(page);
			expect(state.decks[deck].loop, 'device cleanup must release its held loop').toBeNull();
			await _dispatch(page, { type: 'unload', deck });
		}
		await controller.dispose();
	});

	test('Mixtour SHIFT LOOP toggles and cancels pending manual entry', async ({ page, request }) => {
		test.setTimeout(240_000);
		const stableId = process.env.PERFORMANCE_E2E_MIXTOUR_TRACK ?? (await _fetchAnalyzedTracks(request))[0].stable_id;
		const grid = (await beatgridFor(request, API_BASE, stableId, []))?.beatgrid.beats.map((beat) => beat.t * 1000) ?? [];
		expect(grid.length).toBeGreaterThan(32);
		await _gotoPerformance(page);
		const controller = await page.evaluateHandle(async ({ glueUrl, mapUrl }) => {
			const glue = await import(glueUrl) as typeof import('../../src/lib/rb/midi/action-glue.svelte');
			const map = await import(mapUrl) as typeof import('../../src/lib/rb/midi/maps/reloop-mixtour-pro');
			return (deck: 1 | 2 | 3 | 4, note: number, pressed: boolean, padChannel = false) => {
				const binding = map.RELOOP_MIXTOUR_PRO_MAP.bindings.find((candidate) =>
					candidate.source.ch === deck + (padChannel ? 4 : 0) && candidate.source.kind === 'note' && candidate.source.id === note);
				if (binding === undefined) throw new Error(`missing real controller binding ${deck}:${note}`);
				glue.handleMidiAction(binding.action, { kind: 'button', pressed, velocity: pressed ? 127 : 0 }, 'mixtour-loop-gesture-proof');
			};
		}, { glueUrl: '/src/lib/rb/midi/action-glue.svelte.ts', mapUrl: '/src/lib/rb/midi/maps/reloop-mixtour-pro.ts' });
		for (const deck of DECK_IDS) {
			const send = async (note: number, pressed = true, padChannel = false) => {
				await controller.evaluate((input, args) => input(args.deck, args.note, args.pressed, args.padChannel),
					{ deck, note, pressed, padChannel });
				return _idleState(page);
			};
			await _dispatch(page, { type: 'load', deck, stable_id: stableId });
			await _dispatch(page, { type: 'seek', deck, position_ms: grid[8] });
			let state = await send(0x40);
			expect(state.decks[deck].loop?.beat_length).toBe(4);
			state = await send(0x40, false);
			expect(state.decks[deck].loop?.engaged).toBe(true);
			state = await send(0x40);
			expect(state.decks[deck].loop).toBeNull();
			await send(0x03); // manual IN
			state = await send(0x40); // cancel manual IN, not start Auto Loop
			expect(state.decks[deck].loop).toBeNull();
			await _dispatch(page, { type: 'seek', deck, position_ms: grid[9] });
			await send(0x03);
			await _dispatch(page, { type: 'seek', deck, position_ms: grid[13] });
			state = await send(0x03);
			expect(state.decks[deck].loop?.in_ms).toBeCloseTo(grid[9], 3);
			expect(state.decks[deck].loop?.out_ms).toBeCloseTo(grid[13], 3);
			await send(0x03); // manual EXIT
			await send(0x04, true, true); // Auto Loop mode
			await send(0x19, true, true); // pad 6 = eight beats
			await send(0x19, true, true); // off again
			state = await send(0x40);
			expect(state.decks[deck].loop?.beat_length, 'reuse the selected Auto Loop length').toBe(8);
			await send(0x40);
			await send(0x03); // an unfinished manual gesture must not survive reload
			await _dispatch(page, { type: 'load', deck, stable_id: stableId });
			await _dispatch(page, { type: 'seek', deck, position_ms: grid[16] });
			state = await send(0x03);
			expect(state.decks[deck].loop).toBeNull();
			await _dispatch(page, { type: 'seek', deck, position_ms: grid[20] });
			state = await send(0x03);
			expect(state.decks[deck].loop?.in_ms).toBeCloseTo(grid[16], 3);
			await send(0x03);
			await _dispatch(page, { type: 'unload', deck });
		}
		await controller.dispose();
	});

	test('Mixtour N button routes EQ knobs into real decoded stems', async ({ page, request }) => {
		test.setTimeout(240_000);
		const requested = process.env.PERFORMANCE_E2E_MIXTOUR_TRACK;
		let candidates: string[];
		if (requested === undefined) {
			const response = await request.get(`${API_BASE}/api/v1/tracks?limit=1000&available=true`);
			expect(response.ok(), 'real library must be readable for stem discovery').toBe(true);
			const tracks = await response.json() as { items: TrackWire[] };
			candidates = tracks.items.filter((track) => track.file_exists).map((track) => track.stable_id);
		} else {
			candidates = [requested];
		}
		let stableId: string | undefined;
		for (const candidate of candidates) {
			const response = await request.get(`${API_BASE}/api/v1/tracks/${candidate}/stems`);
			expect(response.ok(), `real stem manifest must be readable for ${candidate}`).toBe(true);
			const manifest = await response.json() as { schema?: number; layout?: string };
			if (manifest.schema === 1 && manifest.layout === 'demucs4') {
				stableId = candidate;
				break;
			}
		}
		expect(stableId, 'UNAVAILABLE: requires a real analyzed Demucs track; set PERFORMANCE_E2E_MIXTOUR_TRACK').toBeTruthy();
		await _gotoPerformance(page);
		const glue = await page.evaluateHandle(async (url) => import(url), '/src/lib/rb/midi/action-glue.svelte.ts');
		const sendN = async (deck: 1 | 2 | 3 | 4, pressed: boolean) => {
			await glue.evaluate((module, { deck, pressed }) => module.handleMidiAction(
				{ type: 'deck_stem_eq_toggle', deck },
				{ kind: 'button', pressed, velocity: pressed ? 127 : 0 }
			), { deck, pressed });
			return _idleState(page);
		};
		const sendEq = async (deck: 1 | 2 | 3 | 4, band: 'high' | 'mid' | 'low', value01: number) => {
			await glue.evaluate((module, { deck, band, value01 }) => module.handleMidiAction(
				{ type: 'mixer_channel', deck, target: 'eq', band },
				{ kind: 'continuous', value01, raw: Math.round(value01 * 127) }
			), { deck, band, value01 });
			return _idleState(page);
		};
		try {
			for (const deck of DECK_IDS) {
				let state = await sendN(deck, true);
				expect(state.mixer.channels[deck].stem_eq_mode, 'empty decks must refuse N').toBe(false);
				await _dispatch(page, { type: 'load', deck, stable_id: stableId! });
				await expect.poll(async () => (await _idleState(page)).decks[deck].stems.status,
					{ timeout: 60_000 }).toBe('ready');
				state = await _idleState(page);
				expect(state.decks[deck].stems.available_controls).toEqual(expect.arrayContaining(['vocal', 'instrumental', 'drums']));
				const originalEq = ['eq_high', 'eq_mid', 'eq_low'].map((field) =>
					state.mixer.channels[deck][field as 'eq_high' | 'eq_mid' | 'eq_low']);
				state = await sendN(deck, true);
				expect(state.mixer.channels[deck].stem_eq_mode).toBe(true);
				state = await sendN(deck, false);
				expect(state.mixer.channels[deck].stem_eq_mode, 'release must not toggle twice').toBe(true);
				expect(await glue.evaluate((module, deck) => module.ledTriggerActive({ kind: 'stem_eq_enabled', deck }), deck)).toBe(true);
				for (const [band, stem] of [['high', 'vocal'], ['mid', 'instrumental'], ['low', 'drums']] as const) {
					for (const value of [0, 1, 0.5]) {
						state = await sendEq(deck, band, value);
						expect(state.decks[deck].stems.controls[stem].gain).toBe(value);
						expect([state.mixer.channels[deck].eq_high, state.mixer.channels[deck].eq_mid,
							state.mixer.channels[deck].eq_low]).toEqual(originalEq);
					}
				}
				state = await sendN(deck, true);
				expect(state.mixer.channels[deck].stem_eq_mode).toBe(false);
				state = await sendEq(deck, 'high', 0);
				expect(state.mixer.channels[deck].eq_high).toBe(0);
				expect(state.decks[deck].stems.controls.vocal.gain).toBe(0.5);
				await sendEq(deck, 'high', 0.5);
				await _dispatch(page, { type: 'unload', deck });
			}
		} finally {
			await glue.dispose();
		}
	});

	for (const deck of DECK_IDS) {
		test(`Mixtour Auto Loop pads toggle the real engine loop on deck ${deck}`, async ({ page, request }) => {
			const stableId = process.env.PERFORMANCE_E2E_MIXTOUR_TRACK;
			const track = stableId === undefined
				? (await _fetchAnalyzedTracks(request))[0]
				: { stable_id: stableId, beatgrid_ms: (await beatgridFor(request, API_BASE, stableId, []))
					?.beatgrid.beats.map((beat) => beat.t * 1000) ?? [] };
			expect(track.beatgrid_ms.length, 'real track must provide at least 32 measured beats').toBeGreaterThanOrEqual(32);
			await _gotoPerformance(page);
			const handler = await page.evaluateHandle(async (moduleUrl) => {
				const glue = await import(moduleUrl);
				return glue.handleMidiAction as typeof import('../../src/lib/rb/midi/action-glue.svelte').handleMidiAction;
			}, '/src/lib/rb/midi/action-glue.svelte.ts');
			const feedback = await page.evaluateHandle(async ({ glueUrl, mapUrl, deck }) => {
				const glue = await import(glueUrl) as typeof import('../../src/lib/rb/midi/action-glue.svelte');
				const map = await import(mapUrl) as typeof import('../../src/lib/rb/midi/maps/reloop-mixtour-pro');
				return () => glue.midiLedFeedback(map.RELOOP_MIXTOUR_PRO_MAP, 'mixtour-real-engine-regression')
					.filter((output) => output.ch === 4 + deck && output.note >= 0x14 && output.note <= 0x23);
			}, { glueUrl: '/src/lib/rb/midi/action-glue.svelte.ts',
				mapUrl: '/src/lib/rb/midi/maps/reloop-mixtour-pro.ts', deck });
			const assertLoopLights = async (mode: 'auto_loop' | 'bounce_loop', activePad: number | null) => {
				const outputs = await feedback.evaluate((read) => read());
				expect(outputs).toHaveLength(16);
				expect(new Set(outputs.map((output) => output.note)).size).toBe(16);
				for (const output of outputs) {
					const pad = ((output.note - 0x14) % 8) + 1;
					const expected = mode === 'auto_loop'
						? (pad === activePad ? 67 : 3) : (pad === activePad ? 75 : 11);
					expect(output.velocity, `pad ${pad} ${mode} feedback`).toBe(expected);
				}
			};
			await _dispatch(page, { type: 'load', deck, stable_id: track.stable_id });
			await _dispatch(page, { type: 'seek', deck, position_ms: track.beatgrid_ms[8] });
			const pressPad = async (
				pad: 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8, pressed: boolean,
				mode: 'auto_loop' | 'bounce_loop' = 'auto_loop', shifted = false
			) => {
				await handler.evaluate((handleMidiAction, { deck, pad, pressed, mode, shifted }) => {
					handleMidiAction(
						{ type: 'controller_pad_mode', deck, mode },
						{ kind: 'button', pressed: true, velocity: 127 }, 'mixtour-real-engine-regression'
					);
					handleMidiAction(
						{ type: 'controller_pad', deck, pad, shifted },
						{ kind: 'button', pressed, velocity: pressed ? 127 : 0 }, 'mixtour-real-engine-regression'
					);
				}, { deck, pad, pressed, mode, shifted });
				return _idleState(page);
			};
			let state = await pressPad(5, true);
			expect(state.decks[deck].loop?.engaged).toBe(true);
			expect(state.decks[deck].loop?.beat_length).toBe(4);
			state = await pressPad(5, false);
			expect(state.decks[deck].loop?.engaged).toBe(true);
			const loopStart = state.decks[deck].loop?.in_ms;
			for (const factor of [0.5, 2] as const) {
				await handler.evaluate((handleMidiAction, { deck, factor }) => {
					handleMidiAction(
						{ type: 'deck_loop_scale', deck, factor },
						{ kind: 'button', pressed: true, velocity: 127 }, 'mixtour-real-engine-regression'
					);
				}, { deck, factor });
				state = await _idleState(page);
				expect(state.decks[deck].loop?.beat_length).toBe(factor === 0.5 ? 2 : 4);
				expect(state.decks[deck].loop?.in_ms).toBe(loopStart);
			}
			state = await pressPad(6, true);
			expect(state.decks[deck].loop?.beat_length).toBe(8);
			state = await pressPad(6, true);
			expect(state.decks[deck].loop?.engaged ?? false).toBe(false);
			state = await pressPad(5, true);
			expect(state.decks[deck].loop?.beat_length).toBe(4);
			state = await pressPad(5, true);
			expect(state.decks[deck].loop?.engaged ?? false).toBe(false);
			// A coarse manual quantize grid must not collapse a beat-loop pad.
			await _dispatch(page, { type: 'quantize_grid', deck, beats: 4 });
			for (const mode of ['auto_loop', 'bounce_loop'] as const) {
				const lengths = mode === 'auto_loop'
					? [0.25, 0.5, 1, 2, 4, 8, 16, 32]
					: [0.03125, 0.0625, 0.125, 0.25, 0.5, 1, 2, 4];
				for (const pad of [1, 2, 3, 4, 5, 6, 7, 8] as const) {
					state = await pressPad(pad, true, mode);
					const loop = state.decks[deck].loop;
					expect(loop?.engaged).toBe(true);
					expect(loop?.beat_length).toBe(lengths[pad - 1]);
					const start = track.beatgrid_ms.findIndex((ms) => Math.abs(ms - loop!.in_ms) < 0.01);
					expect(start).toBeGreaterThanOrEqual(0);
					const end = start + lengths[pad - 1];
					const floor = Math.floor(end);
					const expectedOut = track.beatgrid_ms[floor] + (end - floor) *
						(track.beatgrid_ms[Math.ceil(end)] - track.beatgrid_ms[floor]);
					expect(loop!.out_ms).toBeCloseTo(expectedOut, 3);
					await assertLoopLights(mode, pad);
					state = await pressPad(pad, false, mode);
					expect(state.decks[deck].loop?.engaged ?? false).toBe(mode === 'auto_loop');
					if (mode === 'auto_loop') {
						state = await pressPad(pad, true, mode);
						expect(state.decks[deck].loop?.engaged ?? false).toBe(false);
					}
					await assertLoopLights(mode, null);
				}
				// Factory SHIFT bank repeats the same actions; cover both ends.
				for (const pad of [1, 8] as const) {
					state = await pressPad(pad, true, mode, true);
					expect(state.decks[deck].loop?.beat_length).toBe(lengths[pad - 1]);
					await assertLoopLights(mode, pad);
					state = await pressPad(pad, false, mode, true);
					expect(state.decks[deck].loop?.engaged ?? false).toBe(mode === 'auto_loop');
					if (mode === 'auto_loop') await pressPad(pad, true, mode, true);
					await assertLoopLights(mode, null);
				}
			}
			await _dispatch(page, { type: 'play', deck, playing: true });
			await _firstPresentedTransportState(page, deck, true);
			await pressPad(1, true, 'bounce_loop');
			state = await _firstPresentedTransportState(page, deck, true);
			expect(state.decks[deck].loop?.beat_length).toBe(0.03125);
			await pressPad(1, false, 'bounce_loop');
			state = await _firstPresentedTransportState(page, deck, true);
			expect(state.decks[deck].loop?.engaged ?? false).toBe(false);
			await handler.evaluate((handleMidiAction, deck) => handleMidiAction(
				{ type: 'controller_pad_mode', deck, mode: 'pitch_cue' },
				{ kind: 'button', pressed: true, velocity: 127 }, 'mixtour-real-engine-regression'
			), deck);
			expect((await feedback.evaluate((read) => read())).every((output) => output.velocity === 0),
				'unavailable mode must not retain old loop-pad colors').toBe(true);
			await _dispatch(page, { type: 'play', deck, playing: false });
			await _firstPresentedTransportState(page, deck, false);
			// This regression checks each deck's pad semantics. Concurrent
			// four-deck endurance belongs to the separate hardware rehearsal.
			await _dispatch(page, { type: 'unload', deck });
			await handler.dispose();
			await feedback.dispose();
		});
	}

	test('defaults are explicit, master is unique, and UI and IPC share state', async ({ page }) => {
		await _gotoPerformance(page);

		for (const deck of DECK_IDS) {
			for (const control of ['quantize', 'beat-sync', 'master-tempo']) {
				const button = _control(page, deck, control).first();
				await expect(button, `deck ${deck} ${control} button`).toBeEnabled();
				await expect(button).toHaveAttribute('aria-pressed', 'true');
				await expect(button).toHaveAttribute('data-state', 'on');
			}
		}

		await expect(page.locator('button[data-performance-control="master"][aria-pressed="true"]')).toHaveCount(0);
		let state = await _query(page);
		expect(state.version).toBe(1);
		expect(() => JSON.stringify(state)).not.toThrow();
		expect(Object.values(state.decks).filter((deck) => deck.is_master)).toHaveLength(0);
		expect(state.master_deck).toBeNull();
		for (const deck of DECK_IDS) expect(state.decks[deck].sync_mode).toBe('bar');

		state = await _dispatch(page, { type: 'sync_mode', deck: 1, mode: 'beat' });
		expect(state.decks[1].sync_mode).toBe('beat');
		state = await _dispatch(page, { type: 'sync_mode', deck: 1, mode: 'bar' });
		expect(state.decks[1].sync_mode).toBe('bar');

		await _control(page, 1, 'quantize').click();
		state = await _idleState(page);
		expect(state.decks['1'].quantize_enabled).toBe(false);
		await expect(_control(page, 1, 'quantize')).toHaveAttribute('aria-pressed', 'false');

		await _dispatch(page, { type: 'quantize', deck: 1, enabled: true });
		await expect(_control(page, 1, 'quantize')).toHaveAttribute('aria-pressed', 'true');
		await expect(_control(page, 1, 'quantize')).toHaveAttribute('data-state', 'on');

		const serialization = await page.evaluate(async () => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) throw new Error('performance IPC is not installed');
			const first = ipc.dispatch({ type: 'quantize', deck: 1, enabled: false });
			const second = ipc.dispatch({ type: 'quantize', deck: 1, enabled: true });
			const during = ipc.query();
			await Promise.all([first, second]);
			// Dispatch return includes command effects before its own pending accounting clears.
			const after = ipc.query();
			return { during, after };
		});
		expect(serialization.during.command_pending).toBe(true);
		expect(serialization.during.command_queued).toBeGreaterThan(0);
		expect(serialization.after.command_pending).toBe(false);
		expect(serialization.after.decks[1].quantize_enabled).toBe(true);

		await expect(
			page.evaluate(async () => {
				const ipc = window.musicDjToolsPerformance;
				if (ipc === undefined) throw new Error('performance IPC is not installed');
				return ipc.dispatch({ type: 'seek', deck: 9, position_ms: 1000 });
			})
		).rejects.toThrow(/deck/i);
	});

	test('real ANLZ drives quantized seek, normalized beat loops, and full-height grid pixels', async ({
		page,
		request
	}, testInfo) => {
		const track = (await _fetchAnalyzedTracks(request))[0];
		if (anlzDiscoveryReport === null) throw new Error('ANLZ discovery report was not recorded');
		await testInfo.attach('real-anlz-discovery.json', {
			body: JSON.stringify(anlzDiscoveryReport, null, 2),
			contentType: 'application/json'
		});
		await page.setViewportSize({ width: 1280, height: 800 });
		await _gotoPerformance(page);
		await _dispatch(page, { type: 'load', deck: 1, stable_id: track.stable_id });

		let state = await _query(page);
		expect(state.decks['1'].beatgrid_ms).toEqual(track.beatgrid_ms);
		const targetBeat = track.beatgrid_ms[12];
		const nextBeat = track.beatgrid_ms[13];
		const offGrid = targetBeat + (nextBeat - targetBeat) * 0.23;

		await _dispatch(page, { type: 'seek', deck: 1, position_ms: offGrid });
		state = await _query(page);
		expect(state.decks['1'].position_ms).toBeCloseTo(targetBeat, 1);

		state = await _dispatch(page, { type: 'beat_loop', deck: 1, beats: 8 });
		expect(state.decks[1].loop).toEqual({
			in_ms: targetBeat,
			out_ms: track.beatgrid_ms[20],
			engaged: true,
			beat_length: 8
		});
		state = await _dispatch(page, { type: 'safety_loop_save', deck: 1 });
		expect(state.decks[1].safety_loop).toEqual({
			in_ms: targetBeat,
			out_ms: track.beatgrid_ms[20],
			beat_length: 8,
			armed: true
		});
		state = await _dispatch(page, { type: 'beat_loop', deck: 1, beats: 4 });
		expect(state.decks[1].loop).toEqual({
			in_ms: targetBeat,
			out_ms: track.beatgrid_ms[16],
			engaged: true,
			beat_length: 4
		});
		expect(state.decks[1].safety_loop).toEqual({
			in_ms: targetBeat,
			out_ms: track.beatgrid_ms[16],
			beat_length: 4,
			armed: true
		});

		state = await _dispatch(page, { type: 'loop', deck: 1, loop: null });
		expect(state.decks[1].loop).toBeNull();
		expect(state.decks[1].safety_loop).toEqual({
			in_ms: targetBeat,
			out_ms: track.beatgrid_ms[16],
			beat_length: 4,
			armed: false
		});
		state = await _dispatch(page, { type: 'safety_loop_arm', deck: 1, armed: true });
		expect(state.decks[1].safety_loop?.armed).toBe(true);

		await _control(page, 1, 'quantize').click();
		await _dispatch(page, { type: 'seek', deck: 1, position_ms: offGrid });
		state = await _query(page);
		expect(state.decks['1'].position_ms).toBeCloseTo(offGrid, 1);

		await _control(page, 1, 'loop').click();
		state = await _idleState(page);
		expect(state.decks[1].loop).toEqual({
			in_ms: targetBeat,
			out_ms: track.beatgrid_ms[16],
			engaged: true,
			beat_length: 4
		});
		await _dispatch(page, { type: 'seek', deck: 1, position_ms: track.beatgrid_ms[20] });
		await _control(page, 1, 'loop-halve').click();
		state = await _idleState(page);
		expect(state.decks[1].loop).toEqual({
			in_ms: targetBeat,
			out_ms: track.beatgrid_ms[14],
			engaged: true,
			beat_length: 2
		});
		await _control(page, 1, 'loop').click();
		state = await _idleState(page);
		expect(state.decks[1].loop).toBeNull();
		await _dispatch(page, { type: 'quantize', deck: 1, enabled: true });
		state = await _dispatch(page, { type: 'seek', deck: 1, position_ms: targetBeat });

		await expect
			.poll(async () => (await _measureBeatGridCanvas(page, track, state.decks[1].position_ms)).full_height_downbeats)
			.toBeGreaterThan(0);
		const gridPixels = await _measureBeatGridCanvas(page, track, state.decks[1].position_ms);
		expect(gridPixels.ordinary_bottom_matches).toBeGreaterThan(0);
		expect(gridPixels.downbeat_bottom_luma - gridPixels.ordinary_bottom_luma).toBeGreaterThan(10);

		const screenshotPath = testInfo.outputPath('performance-1280x800.png');
		await page.screenshot({ path: screenshotPath, fullPage: false });
		await testInfo.attach('performance-1280x800.png', {
			path: screenshotPath,
			contentType: 'image/png'
		});

		const belowLoopPosition = track.beatgrid_ms[4];
		const multiSpanAboveLoopPosition = track.beatgrid_ms[29];
		const loopIn = track.beatgrid_ms[12];
		const loopOut = track.beatgrid_ms[16];
		const belowLoopExpected = _normalizeLoopPositionMs(belowLoopPosition, loopIn, loopOut);
		const multiSpanExpected = _normalizeLoopPositionMs(
			multiSpanAboveLoopPosition,
			loopIn,
			loopOut
		);
		state = await _dispatch(page, { type: 'seek', deck: 1, position_ms: belowLoopPosition });
		expect(state.decks[1].position_ms).toBeCloseTo(belowLoopPosition, 1);
		expect(state.decks[1].position_ms).toBeLessThan(loopIn);
		state = await _dispatch(page, {
			type: 'loop',
			deck: 1,
			loop: { in_ms: loopIn, out_ms: loopOut }
		});
		expect(state.decks[1].loop).toEqual({
			in_ms: loopIn,
			out_ms: loopOut,
			engaged: true,
			beat_length: null
		});

		state = await _dispatch(page, { type: 'play', deck: 1, playing: true });
		expect(state.decks[1].playing).toBe(true);
		expect(state.decks[1].audible).toBe(false);
		expect(state.decks[1].transport_pending).toBe(true);
		expect(state.decks[1].position_ms).toBeCloseTo(belowLoopPosition, 1);
		await expect.poll(async () => (await _query(page)).master_deck).toBe(1);
		await expect(page.locator('button[data-performance-control="master"][aria-pressed="true"]')).toHaveCount(1);
		let liveLoopState = await _firstPresentedTransportState(page, 1, true);
		expect(liveLoopState.decks[1].position_ms).toBeGreaterThanOrEqual(loopIn);
		expect(liveLoopState.decks[1].position_ms).toBeLessThan(loopOut);
		const belowLoopDistance = _cyclicLoopDistanceMs(
			liveLoopState.decks[1].position_ms,
			belowLoopExpected,
			loopIn,
			loopOut
		);
		expect(
			belowLoopDistance,
			JSON.stringify({
				actual_ms: liveLoopState.decks[1].position_ms,
				expected_ms: belowLoopExpected,
				loop_in_ms: loopIn,
				loop_out_ms: loopOut,
				transport_clock: liveLoopState.decks[1].transport_clock
			})
		).toBeLessThan(50);

		await _dispatch(page, { type: 'play', deck: 1, playing: false });
		await expect
			.poll(async () => {
				const stopped = await _query(page);
				return !stopped.decks[1].playing && !stopped.decks[1].audible;
			})
			.toBe(true);
		state = await _dispatch(page, {
			type: 'seek',
			deck: 1,
			position_ms: multiSpanAboveLoopPosition
		});
		expect(state.decks[1].position_ms).toBeCloseTo(multiSpanAboveLoopPosition, 1);
		expect(state.decks[1].position_ms).toBeGreaterThan(loopOut + (loopOut - loopIn) * 2);
		state = await _dispatch(page, { type: 'play', deck: 1, playing: true });
		expect(state.decks[1].playing).toBe(true);
		expect(state.decks[1].audible).toBe(false);
		expect(state.decks[1].transport_pending).toBe(true);
		expect(state.decks[1].position_ms).toBeCloseTo(multiSpanAboveLoopPosition, 1);
		liveLoopState = await _firstPresentedTransportState(page, 1, true);
		expect(liveLoopState.decks[1].position_ms).toBeGreaterThanOrEqual(loopIn);
		expect(liveLoopState.decks[1].position_ms).toBeLessThan(loopOut);
		const multiSpanDistance = _cyclicLoopDistanceMs(
			liveLoopState.decks[1].position_ms,
			multiSpanExpected,
			loopIn,
			loopOut
		);
		expect(
			multiSpanDistance,
			JSON.stringify({
				actual_ms: liveLoopState.decks[1].position_ms,
				expected_ms: multiSpanExpected,
				loop_in_ms: loopIn,
				loop_out_ms: loopOut,
				transport_clock: liveLoopState.decks[1].transport_clock
			})
		).toBeLessThan(50);

		await _dispatch(page, {
			type: 'seek',
			deck: 1,
			position_ms: multiSpanAboveLoopPosition
		});
		liveLoopState = await _firstPresentedTransportState(page, 1, true);
		expect(liveLoopState.decks[1].position_ms).toBeGreaterThanOrEqual(loopIn);
		expect(liveLoopState.decks[1].position_ms).toBeLessThan(loopOut);
		expect(
			_cyclicLoopDistanceMs(
				liveLoopState.decks[1].position_ms,
				multiSpanExpected,
				loopIn,
				loopOut
			)
		).toBeLessThan(50);

		const replacementLoopIn = track.beatgrid_ms[20];
		const replacementLoopOut = track.beatgrid_ms[24];
		const beforeLiveSetLoop = await page.evaluate(() => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) throw new Error('performance IPC is not installed');
			const contextTime = ipc.capture(1).presentation_context_time_s;
			const deck = ipc.query().decks[1];
			return {
				presentation_context_time_s: contextTime,
				position_ms: deck.position_ms,
				pitch: deck.pitch
			};
		});
		await _dispatch(page, {
			type: 'loop',
			deck: 1,
			loop: { in_ms: replacementLoopIn, out_ms: replacementLoopOut }
		});
		await expect
			.poll(async () => !(await _query(page)).decks[1].transport_pending)
			.toBe(true);
		const afterLiveSetLoop = await page.evaluate(() => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) throw new Error('performance IPC is not installed');
			const contextTime = ipc.capture(1).presentation_context_time_s;
			return { presentation_context_time_s: contextTime, state: ipc.query() };
		});
		liveLoopState = afterLiveSetLoop.state;
		expect(liveLoopState.decks[1].loop).toEqual({
			in_ms: replacementLoopIn,
			out_ms: replacementLoopOut,
			engaged: true,
			beat_length: null
		});
		expect(liveLoopState.decks[1].position_ms).toBeGreaterThanOrEqual(replacementLoopIn);
		expect(liveLoopState.decks[1].position_ms).toBeLessThan(replacementLoopOut);
		const replacementExpected = _normalizeLoopPositionMs(
			beforeLiveSetLoop.position_ms +
				(afterLiveSetLoop.presentation_context_time_s -
					beforeLiveSetLoop.presentation_context_time_s) *
					beforeLiveSetLoop.pitch *
					1000,
			replacementLoopIn,
			replacementLoopOut
		);
		expect(
			_cyclicLoopDistanceMs(
				liveLoopState.decks[1].position_ms,
				replacementExpected,
				replacementLoopIn,
				replacementLoopOut
			)
		).toBeLessThan(50);

		const captureAudit = await page.evaluate(() => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) throw new Error('performance IPC is not installed');
			const capture = ipc.capture(1);
			const values = [
				capture.render_context_time_s,
				capture.presentation_context_time_s,
				capture.sample_rate_hz,
				capture.fft_size,
				...capture.frequency_db,
				...capture.time_domain
			];
			return {
				all_finite: values.every((value) => Number.isFinite(value)),
				frequency_bins: capture.frequency_db.length,
				time_samples: capture.time_domain.length,
				json_has_null: JSON.stringify(capture).includes('null')
			};
		});
		expect(captureAudit.all_finite).toBe(true);
		expect(captureAudit.frequency_bins).toBeGreaterThan(0);
		expect(captureAudit.time_samples).toBeGreaterThan(0);
		expect(captureAudit.json_has_null).toBe(false);
		await _control(page, 1, 'play').click();
		await expect
			.poll(async () => {
				const stopped = await _query(page);
				return {
					playing: stopped.decks[1].playing,
					audible: stopped.decks[1].audible,
					master: stopped.master_deck
				};
			})
			.toEqual({ playing: false, audible: false, master: null });
	});

	// A beat jump while a loop is engaged must move the loop
	// forward/backward by the jump amount (not disengage it), keeping loop
	// length intact, both while paused and while playing.
	test('beat jump shifts a paused, then playing, engaged loop by the jump amount', async ({
		page,
		request
	}) => {
		const track = (await _fetchAnalyzedTracks(request))[0];
		await _gotoPerformance(page);
		await _dispatch(page, { type: 'load', deck: 1, stable_id: track.stable_id });

		const loopIn = track.beatgrid_ms[12];
		const loopOut = track.beatgrid_ms[16];
		const shiftedIn = track.beatgrid_ms[16];
		const shiftedOut = track.beatgrid_ms[20];

		await _dispatch(page, { type: 'seek', deck: 1, position_ms: loopIn });
		let state = await _dispatch(page, { type: 'beat_loop', deck: 1, beats: 4 });
		expect(state.decks[1].loop).toEqual({
			in_ms: loopIn,
			out_ms: loopOut,
			engaged: true,
			beat_length: 4
		});

		// Paused: jumping forward by the loop's own length shifts the loop
		// forward one whole loop, landing the playhead at the new loop-in.
		state = await _dispatch(page, { type: 'beat_jump', deck: 1, beats: 4 });
		expect(state.decks[1].loop).toEqual({
			in_ms: shiftedIn,
			out_ms: shiftedOut,
			engaged: true,
			beat_length: 4
		});
		expect(state.decks[1].position_ms).toBeCloseTo(shiftedIn, 1);

		// Jumping back by the same amount restores the original loop.
		state = await _dispatch(page, { type: 'beat_jump', deck: 1, beats: -4 });
		expect(state.decks[1].loop).toEqual({
			in_ms: loopIn,
			out_ms: loopOut,
			engaged: true,
			beat_length: 4
		});
		expect(state.decks[1].position_ms).toBeCloseTo(loopIn, 1);

		// Playing: the same forward jump shifts the live loop without
		// disengaging it, and the audible transport stays inside the new range.
		state = await _dispatch(page, { type: 'play', deck: 1, playing: true });
		expect(state.decks[1].playing).toBe(true);
		await expect.poll(async () => (await _query(page)).decks[1].audible).toBe(true);

		state = await _dispatch(page, { type: 'beat_jump', deck: 1, beats: 4 });
		expect(state.decks[1].loop).toEqual({
			in_ms: shiftedIn,
			out_ms: shiftedOut,
			engaged: true,
			beat_length: 4
		});
		let liveState = await _firstPresentedTransportState(page, 1, true);
		expect(liveState.decks[1].position_ms).toBeGreaterThanOrEqual(shiftedIn);
		expect(liveState.decks[1].position_ms).toBeLessThan(shiftedOut);

		await _dispatch(page, { type: 'play', deck: 1, playing: false });
	});

	// requirement: DEVLOOP-09
	test(
		'scrolling waveform drag and audio transport share one authoritative position',
		{ tag: '@performance-smoke' },
		async ({ page, request }) => {
		const track = (await _fetchAnalyzedTracks(request))[0];
		const originMs = track.beatgrid_ms[24];
		await _gotoPerformance(page);
		await _dispatch(page, { type: 'load', deck: 1, stable_id: track.stable_id });
		const decodedDurationMs = await page.evaluate(async (stableId) => {
			const response = await fetch(
				`/api/v1/tracks/${encodeURIComponent(stableId)}/audio`,
				{ cache: 'no-store' }
			);
			if (!response.ok) throw new Error(`duration fixture failed with HTTP ${response.status}`);
			const decoder = new OfflineAudioContext(1, 1, 44_100);
			const decoded = await decoder.decodeAudioData(await response.arrayBuffer());
			return decoded.duration * 1000;
		}, track.stable_id);
		const publicDurationMs = (await _query(page)).decks[1].duration_ms;
		if (publicDurationMs === null) throw new Error('loaded real track has no decoded duration');
		// Separate Web Audio contexts can resample to different device rates.
		// Duration must agree within one sample at the lowest supported rate.
		expect(Math.abs(publicDurationMs - decodedDurationMs)).toBeLessThanOrEqual(1000 / 8000);
		await _dispatch(page, { type: 'quantize', deck: 1, enabled: false });
		await _dispatch(page, { type: 'seek', deck: 1, position_ms: originMs });

		const waveform = page.locator('.rb-waverow canvas[aria-label="deck 1 waveform seek"]');
		await expect(waveform).toHaveAttribute('aria-valuenow', `${Math.round(originMs)}`);
		const bounds = await waveform.boundingBox();
		if (bounds === null) throw new Error('deck 1 scrolling waveform is not visible');
		const dragPixels = Math.min(100, bounds.width / 4);
		const startX = bounds.x + bounds.width / 2;
		const pointerY = bounds.y + bounds.height / 2;
		const expectedMs = originMs - (dragPixels / bounds.width) * 24_000;

		// No delay between movement and release. This reproduces the historical
		// lost-release bug where the first seek kept command_pending true.
		await page.mouse.move(startX, pointerY);
		await page.mouse.down();
		await page.mouse.move(startX + dragPixels, pointerY, { steps: 1 });
		await page.mouse.up();

		const settled = await _idleState(page);
		expect(settled.decks[1].position_ms).toBeCloseTo(expectedMs, 1);
		expect(settled.decks[1].transport_clock).toEqual({
			source: 'paused_cursor',
			presentation_context_time_s: null,
			desired_revision: 0,
			presented_revision: 0
		});
		await expect(waveform).toHaveAttribute(
			'aria-valuenow',
			`${Math.round(settled.decks[1].position_ms)}`
		);

		const acceptedPlay = await _dispatch(page, { type: 'play', deck: 1, playing: true });
		expect(acceptedPlay.decks[1].transport_pending).toBe(true);
		expect(acceptedPlay.decks[1].transport_clock.desired_revision).toBeGreaterThan(
			acceptedPlay.decks[1].transport_clock.presented_revision
		);
		const presentedPlay = await _firstPresentedTransportState(page, 1, true);
		expect(presentedPlay.decks[1].transport_clock.source).toBe('audio_output');
		expect(presentedPlay.decks[1].transport_clock.presentation_context_time_s).not.toBeNull();
		expect(presentedPlay.decks[1].transport_clock.presented_revision).toBe(
			presentedPlay.decks[1].transport_clock.desired_revision
		);
		await expect.poll(async () => _waveformIpcPositionDeltaMs(page, 1)).toBeLessThan(35);
		let live = await _query(page);
		expect(live.decks[1].position_ms).toBeGreaterThan(settled.decks[1].position_ms);

		const liveOriginMs = live.decks[1].position_ms;
		const liveExpectedMs = liveOriginMs - (dragPixels / bounds.width) * 24_000;
		const intermediatePixels = dragPixels / 2;
		await page.mouse.move(startX, pointerY);
		await page.mouse.down();
		await page.waitForTimeout(100);
		const sawInFlightSeek = page.evaluate(async () => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) throw new Error('performance IPC is not installed');
			const deadline = performance.now() + 5_000;
			while (performance.now() < deadline) {
				if (ipc.query().command_pending) return true;
				await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
			}
			return false;
		});
		await page.mouse.move(startX + intermediatePixels, pointerY, { steps: 1 });
		expect(await sawInFlightSeek).toBe(true);
		await page.mouse.move(startX + dragPixels, pointerY, { steps: 1 });
		await page.mouse.up();
		await expect.poll(async () => (await _query(page)).decks[1].transport_pending).toBe(true);
		await expect
			.poll(async () => {
				const clock = (await _query(page)).decks[1].transport_clock;
				return clock.desired_revision > clock.presented_revision;
			})
			.toBe(true);
		live = await _firstPresentedTransportState(page, 1, true);
		expect(live.decks[1].transport_clock.source).toBe('audio_output');
		expect(live.decks[1].transport_clock.presented_revision).toBe(
			live.decks[1].transport_clock.desired_revision
		);
		expect(live.decks[1].position_ms).toBeGreaterThanOrEqual(liveExpectedMs);
		expect(live.decks[1].position_ms - liveExpectedMs).toBeLessThan(50);
		await expect.poll(async () => _waveformIpcPositionDeltaMs(page, 1)).toBeLessThan(35);
		const capture = await page.evaluate(() => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) throw new Error('performance IPC is not installed');
			return ipc.capture(1);
		});
		expect(
			[
				capture.render_context_time_s,
				capture.presentation_context_time_s,
				...capture.frequency_db,
				...capture.time_domain
			].every((value) => Number.isFinite(value))
		).toBe(true);

		const durationMs = live.decks[1].duration_ms;
		if (durationMs === null) throw new Error('loaded real track has no decoded duration');
		await _dispatch(page, {
			type: 'seek',
			deck: 1,
			position_ms: Math.max(0, durationMs - 250)
		});
		await expect
			.poll(async () => {
				const state = await _query(page);
				const deck = state.decks[1];
				return (
					!deck.playing &&
					!deck.audible &&
					!deck.transport_pending &&
					deck.transport_clock.presented_revision === deck.transport_clock.desired_revision
				);
			})
			.toBe(true);
		const ended = await _query(page);
		expect(ended.decks[1].position_ms).toBeCloseTo(durationMs, 3);
		expect(ended.master_deck).toBeNull();
	});

	// requirement: DEVLOOP-09
	test(
		'real decoded tracks synchronize tempo and phase to one master',
		{ tag: '@performance-smoke' },
		async ({ page, request }) => {
		const [masterTrack, followerTrack] = await _realSyncPair(request);
		await _gotoPerformance(page);
		await _dispatch(page, { type: 'load', deck: 1, stable_id: masterTrack.stable_id });
		await _dispatch(page, { type: 'load', deck: 2, stable_id: followerTrack.stable_id });
		await _control(page, 2, 'master').click();
		let masterState = await _idleState(page);
		expect(masterState.master_deck).toBe(2);
		expect(Object.values(masterState.decks).filter((deck) => deck.is_master)).toHaveLength(1);
		await _dispatch(page, { type: 'master', deck: 1 });
		masterState = await _query(page);
		expect(masterState.master_deck).toBe(1);
		expect(Object.values(masterState.decks).filter((deck) => deck.is_master)).toHaveLength(1);

		await _dispatch(page, { type: 'seek', deck: 1, position_ms: masterTrack.beatgrid_ms[12] });
		await _control(page, 1, 'play').click();
		await expect
			.poll(async () => {
				const active = await _query(page);
				return active.decks[1].audible && !active.decks[1].transport_pending;
			})
			.toBe(true);

		const pausedFollowerBeatIndex = 20;
		const pausedFollowerBeat = followerTrack.beatgrid_ms[pausedFollowerBeatIndex];
		let followerState = await _dispatch(page, {
			type: 'seek',
			deck: 2,
			position_ms: pausedFollowerBeat
		});
		expect(followerState.decks[2].playing).toBe(false);
		expect(followerState.decks[2].audible).toBe(false);
		expect(followerState.decks[2].position_ms).toBeCloseTo(pausedFollowerBeat, 1);
		await page.waitForTimeout(100);
		followerState = await _query(page);
		expect(followerState.decks[2].position_ms).toBeCloseTo(pausedFollowerBeat, 1);

		await _control(page, 2, 'play').click();
		await expect
			.poll(async () => {
				const active = await _query(page);
				return (
					active.decks[1].audible &&
					active.decks[2].audible &&
					!active.decks[1].transport_pending &&
					!active.decks[2].transport_pending
				);
			})
			.toBe(true);
		followerState = await _query(page);
		expect(
			Math.abs(_nearestBeatIndex(followerState.decks[2]) - pausedFollowerBeatIndex)
		).toBeLessThanOrEqual(3);

		await expect
			.poll(async () => {
				const state = await _query(page);
				const master = state.decks['1'];
				const follower = state.decks['2'];
				if (master.effective_bpm === null || follower.effective_bpm === null) return Infinity;
				return Math.abs(master.effective_bpm - follower.effective_bpm);
			})
			.toBeLessThan(0.02);

		await expect
			.poll(async () => {
				const state = await _query(page);
				return Math.abs(
					_timeToNextBeatMs(state.decks['1']) - _timeToNextBeatMs(state.decks['2'])
				);
			})
			.toBeLessThan(30);

		const observedBeatNumbers = new Set<number>();
		const barParityDeadline = Date.now() + 5_000;
		while (observedBeatNumbers.size < 4 && Date.now() < barParityDeadline) {
			const barState = await _query(page);
			const masterCoordinate = _beatCoordinate(
				masterTrack,
				barState.decks[1].position_ms
			);
			if (masterCoordinate.phase >= 0.2 && masterCoordinate.phase <= 0.8) {
				const followerCoordinate = _beatCoordinate(
					followerTrack,
					barState.decks[2].position_ms
				);
				expect(followerCoordinate.n).toBe(masterCoordinate.n);
				expect(Math.abs(followerCoordinate.phase - masterCoordinate.phase)).toBeLessThan(0.08);
				observedBeatNumbers.add(masterCoordinate.n);
			}
			await page.waitForTimeout(16);
		}
		expect([...observedBeatNumbers].sort()).toEqual([1, 2, 3, 4]);

		const audibleFollowerBeatIndex = 27;
		await _dispatch(page, {
			type: 'seek',
			deck: 2,
			position_ms: followerTrack.beatgrid_ms[audibleFollowerBeatIndex]
		});
		await expect
			.poll(async () => {
				const active = await _query(page);
				return (
					active.decks[2].audible &&
					active.decks[2].playing &&
					!active.decks[2].transport_pending
				);
			})
			.toBe(true);
		followerState = await _query(page);
		expect(
			Math.abs(_nearestBeatIndex(followerState.decks[2]) - audibleFollowerBeatIndex)
		).toBeLessThanOrEqual(3);
		expect(
			Math.abs(
				_timeToNextBeatMs(followerState.decks[1]) -
					_timeToNextBeatMs(followerState.decks[2])
			)
		).toBeLessThan(30);

		const rateTransition = await page.evaluate(async (masterRate) => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) throw new Error('performance IPC is not installed');
			let sawBothPending = false;
			let pendingMismatchAfterSharedPending = false;
			const settledPhaseDeltasMs: number[] = [];

			function phaseDeltaMs(): number | null {
				const state = ipc?.query();
				if (state === undefined) throw new Error('performance IPC disappeared during rate change');
				const master = state.decks[1];
				const follower = state.decks[2];
				const nextMaster = master.beatgrid_ms.find((beat) => beat > master.position_ms + 0.5);
				const nextFollower = follower.beatgrid_ms.find((beat) => beat > follower.position_ms + 0.5);
				if (nextMaster === undefined || nextFollower === undefined) return null;
				return Math.abs(
					(nextMaster - master.position_ms) / master.pitch -
						(nextFollower - follower.position_ms) / follower.pitch
				);
			}

			function sample(): void {
				const state = ipc?.query();
				if (state === undefined) throw new Error('performance IPC disappeared during rate sampling');
				const masterPending = state.decks[1].transport_pending;
				const followerPending = state.decks[2].transport_pending;
				if (masterPending && followerPending) sawBothPending = true;
				// The follower re-anchors its tempo through a short rate ramp, so it
				// legitimately clears transport_pending later than the master (which
				// applies its own tempo change in one step) - that ordering is
				// expected, not a mismatch. Only the reverse - follower settling
				// while master is still catching up - would be a cross-deck
				// serialization regression.
				else if (sawBothPending && !followerPending && masterPending) {
					pendingMismatchAfterSharedPending = true;
				}
				if (sawBothPending && !masterPending && !followerPending) {
					const delta = phaseDeltaMs();
					if (delta !== null) settledPhaseDeltasMs.push(delta);
				}
			}

			const timer = window.setInterval(sample, 4);
			try {
				await ipc.dispatch({ type: 'tempo', deck: 1, ratio: masterRate });
				const deadline = performance.now() + 5_000;
				while (performance.now() < deadline) {
					sample();
					const state = ipc.query();
					if (
						sawBothPending &&
						!state.decks[1].transport_pending &&
						!state.decks[2].transport_pending
					) {
						break;
					}
					await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
				}
				await new Promise<void>((resolve) => window.setTimeout(resolve, 80));
				sample();
				return {
					saw_both_pending: sawBothPending,
					pending_mismatch_after_shared_pending: pendingMismatchAfterSharedPending,
					settled_phase_deltas_ms: settledPhaseDeltasMs,
					after: ipc.query()
				};
			} finally {
				window.clearInterval(timer);
			}
		}, SYNC_MASTER_RATE);
		expect(rateTransition.saw_both_pending).toBe(true);
		expect(rateTransition.pending_mismatch_after_shared_pending).toBe(false);
		expect(rateTransition.settled_phase_deltas_ms.length).toBeGreaterThan(0);
		expect(Math.max(...rateTransition.settled_phase_deltas_ms)).toBeLessThan(30);
		expect(rateTransition.after.decks[1].pitch).toBeCloseTo(SYNC_MASTER_RATE, 5);
		expect(rateTransition.after.decks[1].effective_bpm).not.toBeNull();
		expect(rateTransition.after.decks[2].effective_bpm).not.toBeNull();
		expect(
			Math.abs(
				(rateTransition.after.decks[1].effective_bpm ?? 0) -
					(rateTransition.after.decks[2].effective_bpm ?? 0)
			)
		).toBeLessThan(0.02);

		const state = await _query(page);
		expect(state.master_deck).toBe(1);
		expect(Object.values(state.decks).filter((deck) => deck.is_master)).toHaveLength(1);
		expect(state.decks['2'].sync_error).toBeNull();
		expect(state.decks['1'].command_error).toBeNull();
		expect(state.decks['2'].command_error).toBeNull();

		const crossDeckSerialization = await page.evaluate(async () => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) throw new Error('performance IPC is not installed');
			const masterRate = ipc.dispatch({ type: 'tempo', deck: 1, ratio: 1.1 });
			const followerPause = ipc.dispatch({ type: 'play', deck: 2, playing: false });
			const during = ipc.query();
			await Promise.all([masterRate, followerPause]);
			const after = ipc.query();
			return { during, after };
		});
		expect(crossDeckSerialization.during.command_pending).toBe(true);
		expect(crossDeckSerialization.during.command_queued).toBeGreaterThan(0);
		expect(crossDeckSerialization.after.command_pending).toBe(false);
		expect(crossDeckSerialization.after.decks[2].playing).toBe(false);
		expect(crossDeckSerialization.after.decks[2].audible).toBe(true);
		expect(crossDeckSerialization.after.decks[2].transport_pending).toBe(true);
		expect(
			crossDeckSerialization.after.decks[2].transport_clock.desired_revision
		).toBeGreaterThan(
			crossDeckSerialization.after.decks[2].transport_clock.presented_revision
		);
		expect(crossDeckSerialization.after.decks[2].sync_error).toBeNull();
		const presentedFollowerPause = await _firstPresentedTransportState(page, 2, false);
		expect(presentedFollowerPause.decks[2].transport_clock.source).toBe('paused_cursor');
		expect(presentedFollowerPause.decks[2].transport_clock.presented_revision).toBe(
			presentedFollowerPause.decks[2].transport_clock.desired_revision
		);

		await expect(_dispatch(page, { type: 'master', deck: 2 })).rejects.toThrow(/master|audible|playing/i);
		const rejectedMasterState = await _query(page);
		expect(rejectedMasterState.master_deck).toBe(1);
		expect(rejectedMasterState.decks[1].audible).toBe(true);
		expect(rejectedMasterState.decks[2].audible).toBe(false);
		expect(rejectedMasterState.decks[2].command_error).toMatch(/master|audible|playing/i);
		const banner = page.locator('[data-performance-error="2"]');
		await expect(banner).toBeVisible();

		// The banner never fades and has its own dismiss control, so it can stand
		// on a deck for an entire set. It used to render the error string and
		// nothing else, which left the words on screen tied to no row in any log:
		// two identical failures an hour apart were indistinguishable. The id it
		// now carries is only worth showing if it is the SAME id the log carries,
		// so that is what is asserted here rather than its mere presence.
		const bannerId = await banner.getAttribute('data-performance-error-id');
		expect(bannerId, 'the deck error banner rendered no id, so it is unfindable in any log').not.toBeNull();
		expect(bannerId, 'the banner id is not a greppable token').toMatch(/^t-\d+$/);
		await expect(
			banner.locator('.deck-error-id'),
			'the id is in the DOM but not shown, so nobody can read it off the screen'
		).toHaveText(bannerId as string);

		const ringRow = await page.evaluate((id) => {
			const w = window as Window & { __mdtPerfLog?: () => { kind: string; message: string; deck: number | null }[] };
			if (w.__mdtPerfLog === undefined) return 'no perf ring on the page';
			const hit = w.__mdtPerfLog().find((row) => row.kind.includes(id));
			return hit === undefined ? null : { kind: hit.kind, message: hit.message, deck: hit.deck };
		}, bannerId as string);
		expect(
			ringRow,
			`no perf-event ring row carries the id the banner shows (${bannerId}), so searching for it leads nowhere`
		).not.toBeNull();
		expect(typeof ringRow === 'object' && ringRow !== null ? ringRow.deck : null).toBe(2);
		expect(
			typeof ringRow === 'object' && ringRow !== null ? ringRow.message : '',
			'the ring row carries the id but not the failure it identifies'
		).toMatch(/master|audible|playing/i);
	});

	test('BAR Beat Sync folds a half-tempo follower with a warn toast and perf row', async ({
		page,
		request
	}) => {
		test.skip(
			!IS_GENERATED_FIXTURE,
			`generated fixture at ${DATA_DIR}: run with PERFORMANCE_E2E_FIXTURE=1 for analysis-backed BAR fold coverage`
		);
		const [masterTrack, followerTrack] = await _barFoldSyncPair(request);
		for (const track of [masterTrack, followerTrack]) {
			const beatOnes = track.beats.filter((beat) => beat.n === 1);
			expect(beatOnes.length, `${track.title} beat-1 markers`).toBeGreaterThan(0);
			expect(
				track.beatgrid_ms.every((beat, index, grid) => index === 0 || beat > grid[index - 1]),
				`${track.title} beatgrid must be strictly increasing`
			).toBe(true);
		}
		await _gotoPerformance(page);
		await _dispatch(page, { type: 'load', deck: 1, stable_id: masterTrack.stable_id });
		await _dispatch(page, { type: 'load', deck: 2, stable_id: followerTrack.stable_id });
		await _dispatch(page, { type: 'master', deck: 1 });
		await _dispatch(page, { type: 'sync_mode', deck: 1, mode: 'bar' });
		await _dispatch(page, { type: 'sync_mode', deck: 2, mode: 'bar' });
		await _dispatch(page, { type: 'beat_sync', deck: 2, enabled: true });
		await expect
			.poll(async () => {
				const state = await _query(page);
				return (
					state.decks[1].beatgrid_ms.length > 31 && state.decks[2].beatgrid_ms.length > 31
				);
			})
			.toBe(true);
		const loaded = await _query(page);
		for (const deckId of [1, 2] as const) {
			expect(
				loaded.decks[deckId].beatgrid_ms.every(
					(beat, index, grid) => index === 0 || beat > grid[index - 1]
				),
				`deck ${deckId} IPC beatgrid`
			).toBe(true);
		}
		await _dispatch(page, { type: 'seek', deck: 1, position_ms: masterTrack.beatgrid_ms[12] });
		await _control(page, 1, 'play').click();
		await expect
			.poll(async () => {
				const state = await _query(page);
				return state.decks[1].audible && !state.decks[1].transport_pending;
			})
			.toBe(true);
		const pausedFollowerBeat = followerTrack.beatgrid_ms[20];
		await _dispatch(page, { type: 'seek', deck: 2, position_ms: pausedFollowerBeat });
		await _control(page, 2, 'play').click();
		await expect
			.poll(async () => {
				const state = await _query(page);
				return (
					state.decks[1].audible &&
					state.decks[2].audible &&
					!state.decks[1].transport_pending &&
					!state.decks[2].transport_pending &&
					state.decks[2].sync_error === null
				);
			})
			.toBe(true);
		await expect
			.poll(async () => {
				const state = await _query(page);
				return _tempoLockedToMaster(
					state.decks[2].effective_bpm,
					state.decks[1].effective_bpm
				);
			})
			.toBe(true);
		await expect
			.poll(async () => {
				const state = await _query(page);
				return Math.abs(
					_timeToNextBeatMs(state.decks[1]) - _timeToNextBeatMs(state.decks[2])
				);
			})
			.toBeLessThan(30);

		const settled = await _query(page);
		expect(settled.decks[1].beatgrid_ms.length).toBeGreaterThan(31);
		expect(settled.decks[2].beatgrid_ms.length).toBeGreaterThan(31);
		expect(settled.decks[2].sync_error).toBeNull();
		expect(_tempoLockedToMaster(settled.decks[2].effective_bpm, settled.decks[1].effective_bpm)).toBe(
			true
		);
		// The same slack as _tempoLockedToMaster: a phase-lock trim moves the follower up to 0.3%.
		expect(Math.abs(settled.decks[1].effective_bpm! - settled.decks[2].effective_bpm! * 2)).toBeLessThan(
			0.1 + 0.003 * settled.decks[1].effective_bpm!
		);

		const foldEvidence = await page.evaluate(() => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) throw new Error('performance IPC is not installed');
			const toasts = ipc.toasts();
			const warnToast = toasts.find(
				(toast) => toast.kind === 'warn' && /tempo fold|half tempo/i.test(toast.message)
			);
			const read = (window as unknown as { __mdtPerfLog?: () => { kind: string; message?: string }[] })
				.__mdtPerfLog;
			const foldRow = read === undefined
				? null
				: read().find((row) => row.kind === 'beat-sync-fold');
			return { warnToast, foldRow };
		});
		expect(foldEvidence.warnToast, 'BAR fold must surface an orange warn toast').toBeTruthy();
		expect(foldEvidence.warnToast?.message, 'warn toast must name the tempo fold').toMatch(
			/half tempo|tempo fold/i
		);
		expect(foldEvidence.foldRow, 'performance ring must record beat-sync-fold').toBeTruthy();
		expect(foldEvidence.foldRow?.kind).toBe('beat-sync-fold');
		expect(foldEvidence.foldRow?.message, 'perf row must name the fold normalization').toMatch(
			/tempoNormalization=0\.5|half/i
		);

		const visibleFollowerBpm = await page
			.getByTestId('tempo-readout-deck-2')
			.textContent();
		const visibleBeatSyncPressed = await page
			.getByTestId('beat-sync-deck-2')
			.getAttribute('aria-pressed');
		expect(visibleFollowerBpm).not.toBeNull();
		expect(settled.decks[2].bpm).not.toBeNull();
		expect(Number.parseFloat(visibleFollowerBpm!)).toBeCloseTo(settled.decks[2].bpm!, 1);
		expect(visibleBeatSyncPressed).toBe(String(settled.decks[2].beat_sync_enabled));
	});

	test('pending Beat Sync follower start follows a newly selected audible master', async ({
		page,
		request
	}) => {
		const [initialMasterTrack, followerTrack] = await _realSyncPair(request);
		await _gotoPerformance(page);
		await _dispatch(page, { type: 'load', deck: 1, stable_id: initialMasterTrack.stable_id });
		await _dispatch(page, { type: 'load', deck: 2, stable_id: followerTrack.stable_id });
		await _dispatch(page, { type: 'load', deck: 3, stable_id: initialMasterTrack.stable_id });
		await _dispatch(page, { type: 'master', deck: 1 });
		await _dispatch(page, { type: 'beat_sync', deck: 3, enabled: false });
		await _dispatch(page, { type: 'quantize', deck: 3, enabled: false });
		await _dispatch(page, {
			type: 'seek',
			deck: 1,
			position_ms: initialMasterTrack.beatgrid_ms[12]
		});
		const deck3OffPhase =
			initialMasterTrack.beatgrid_ms[16] +
			(initialMasterTrack.beatgrid_ms[17] - initialMasterTrack.beatgrid_ms[16]) * 0.5;
		await _dispatch(page, {
			type: 'seek',
			deck: 3,
			position_ms: deck3OffPhase
		});
		await _dispatch(page, {
			type: 'seek',
			deck: 2,
			position_ms: followerTrack.beatgrid_ms[20]
		});
		await _dispatch(page, { type: 'play', deck: 1, playing: true });
		await expect
			.poll(async () => {
				const state = await _query(page);
				return state.decks[1].audible && !state.decks[1].transport_pending;
			})
			.toBe(true);
		await _dispatch(page, { type: 'play', deck: 3, playing: true });
		await expect
			.poll(async () => {
				const state = await _query(page);
				return state.decks[3].audible && !state.decks[3].transport_pending;
			})
			.toBe(true);
		const beforeSwitch = await _query(page);
		expect(beforeSwitch.master_deck).toBe(1);
		expect(beforeSwitch.decks[3].beat_sync_enabled).toBe(false);
		expect(beforeSwitch.decks[3].quantize_enabled).toBe(false);
		expect(
			Math.abs(
				_timeToNextBeatMs(beforeSwitch.decks[1]) - _timeToNextBeatMs(beforeSwitch.decks[3])
			)
		).toBeGreaterThan(100);
		const initialFollowerPitch = initialMasterTrack.bpm / followerTrack.bpm;
		await _dispatch(page, { type: 'tempo', deck: 2, ratio: initialFollowerPitch });
		await _dispatch(page, { type: 'quantize', deck: 2, enabled: false });
		const initiallyAligned = await page.evaluate(
			async ({ followerAnchorMs }) => {
				const ipc = window.musicDjToolsPerformance;
				if (ipc === undefined) throw new Error('performance IPC is not installed');
				const before = ipc.query();
				const master = before.decks[1];
				const nextMasterBeat = master.beatgrid_ms.find(
					(beat) => beat > master.position_ms + 0.5
				);
				if (nextMasterBeat === undefined) {
					throw new Error(`deck 1 has no next beat at ${master.position_ms}`);
				}
				const masterTimeToNextBeatMs = (nextMasterBeat - master.position_ms) / master.pitch;
				return ipc.dispatch({
					type: 'seek',
					deck: 2,
					position_ms:
						followerAnchorMs - masterTimeToNextBeatMs * before.decks[2].pitch
				});
			},
			{ followerAnchorMs: followerTrack.beatgrid_ms[20] }
		);
		expect(initiallyAligned.decks[2].playing).toBe(false);
		expect(initiallyAligned.decks[2].beat_sync_enabled).toBe(true);
		expect(initiallyAligned.decks[2].pitch).toBeCloseTo(initialFollowerPitch, 3);
		expect(
			Math.abs(
				_timeToNextBeatMs(initiallyAligned.decks[1]) -
					_timeToNextBeatMs(initiallyAligned.decks[2])
			)
		).toBeLessThan(30);

		const switchAudit = await page.evaluate(async () => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) throw new Error('performance IPC is not installed');
			const followerStart = await ipc.dispatch({ type: 'play', deck: 2, playing: true });
			const switched = await ipc.dispatch({ type: 'master', deck: 3 });
			return { followerStart, switched };
		});
		expect(switchAudit.followerStart.master_deck).toBe(1);
		expect(switchAudit.followerStart.decks[2].playing).toBe(true);
		expect(switchAudit.followerStart.decks[2].audible).toBe(false);
		expect(switchAudit.followerStart.decks[2].transport_pending).toBe(true);
		expect(switchAudit.followerStart.decks[2].beat_sync_enabled).toBe(true);
		expect(switchAudit.followerStart.decks[2].pitch).toBeCloseTo(initialFollowerPitch, 3);
		expect(switchAudit.followerStart.decks[2].sync_error).toBeNull();
		expect(switchAudit.switched.master_deck).toBe(3);
		expect(switchAudit.switched.decks[2].playing).toBe(true);
		expect(switchAudit.switched.decks[2].transport_pending).toBe(true);

		await expect
			.poll(async () => {
				const state = await _query(page);
				return (
					state.master_deck === 3 &&
					state.decks[2].audible &&
					state.decks[3].audible &&
					!state.decks[2].transport_pending &&
					!state.decks[3].transport_pending
				);
			})
			.toBe(true);
		const settled = await _query(page);
		expect(settled.decks[2].sync_error).toBeNull();
		expect(settled.decks[3].sync_error).toBeNull();
		expect(
			Math.abs(_timeToNextBeatMs(settled.decks[3]) - _timeToNextBeatMs(settled.decks[2]))
		).toBeLessThan(30);
	});

	test('audible replacement fails and overlapping stopped loads publish only the latest candidate', async ({
		page,
		request
	}) => {
		const tracks = await _fetchAnalyzedTracks(request);
		expect(tracks.length, 'overlapping-load regression requires three real analyzed tracks').toBeGreaterThan(2);
		const [candidateA, candidateB, incumbent] = tracks;
		await _gotoPerformance(page);
		await _dispatch(page, { type: 'load', deck: 4, stable_id: incumbent.stable_id });
		await _dispatch(page, { type: 'play', deck: 4, playing: true });
		await expect
			.poll(async () => {
				const state = await _query(page);
				return state.decks[4].audible && !state.decks[4].transport_pending;
			})
			.toBe(true);
		await expect(
			_dispatch(page, { type: 'load', deck: 4, stable_id: candidateA.stable_id })
		).rejects.toThrow(/fully stopped/i);
		let protectedIncumbent = await _query(page);
		expect(protectedIncumbent.decks[4].stable_id).toBe(incumbent.stable_id);
		expect(protectedIncumbent.decks[4].audible).toBe(true);
		expect(protectedIncumbent.decks[4].transport_pending).toBe(false);

		await _dispatch(page, { type: 'play', deck: 4, playing: false });
		await _firstPresentedTransportState(page, 4, false);
		const activationRace = await page.evaluate(
			async ({ incumbentId, candidateAId, moduleUrl }) => {
				const audio = (await import(moduleUrl)) as {
					engine: {
						load(deck: 4, stableId: string): Promise<void>;
						play(deck: 4): Promise<void>;
					};
					getDeckState(deck: 4): {
						stable_id: string | null;
						playing: boolean;
					};
				};
				if (audio.getDeckState(4).stable_id !== incumbentId) {
					throw new Error('dynamic engine import does not share the incumbent deck singleton');
				}
				const candidateLoad = audio.engine.load(4, candidateAId);
				const incumbentPlay = audio.engine.play(4);
				const outcomes = await Promise.allSettled([candidateLoad, incumbentPlay]);
				const state = audio.getDeckState(4);
				return {
					outcomes: outcomes.map((outcome) =>
						outcome.status === 'fulfilled'
							? 'fulfilled'
							: `rejected: ${String(outcome.reason)}`
					),
					stable_id: state.stable_id,
					playing: state.playing
				};
			},
			{
				incumbentId: incumbent.stable_id,
				candidateAId: candidateA.stable_id,
				moduleUrl: '/src/lib/rb/audio-engine.svelte.ts'
			}
		);
		expect(activationRace.outcomes[0]).toMatch(/rejected:.*fully stopped/i);
		expect(activationRace.outcomes[1]).toBe('fulfilled');
		expect(activationRace.stable_id).toBe(incumbent.stable_id);
		expect(activationRace.playing).toBe(true);
		await _firstPresentedTransportState(page, 4, true);
		await _dispatch(page, { type: 'play', deck: 4, playing: false });
		await _firstPresentedTransportState(page, 4, false);

		const overlap = await page.evaluate(
			async ({ incumbentId, candidateAId, candidateBId, moduleUrl }) => {
				const audio = (await import(moduleUrl)) as {
					engine: {
						load(deck: 4, stableId: string): Promise<void>;
					};
					getDeckState(deck: 4): {
						stable_id: string | null;
						audible: boolean;
						processor_error: string | null;
						duration_ms: number | null;
						anlz: { beatgrid: { beats: unknown[] } } | null;
					};
				};
				const samples: Array<string | null> = [];
				const sample = () => samples.push(audio.getDeckState(4).stable_id);
				const incumbentBefore = audio.getDeckState(4);
				if (incumbentBefore.stable_id !== incumbentId || incumbentBefore.audible) {
					throw new Error('dynamic engine import does not share the incumbent deck singleton');
				}
				sample();
				const first = audio.engine.load(4, candidateAId);
				const latest = audio.engine.load(4, candidateBId);
				const incumbentDuringCandidatePreparation = audio.getDeckState(4);
				if (
					incumbentDuringCandidatePreparation.stable_id !== incumbentId ||
					incumbentDuringCandidatePreparation.audible
				) {
					throw new Error('a candidate replaced the incumbent before asynchronous preparation');
				}
				const timer = window.setInterval(sample, 2);
				try {
					const outcomes = await Promise.allSettled([first, latest]);
					sample();
					const finalState = audio.getDeckState(4);
					return {
						samples,
						outcomes: outcomes.map((outcome) =>
							outcome.status === 'fulfilled'
								? 'fulfilled'
								: `rejected: ${String(outcome.reason)}`
						),
						final: {
							stable_id: finalState.stable_id,
							audible: finalState.audible,
							processor_error: finalState.processor_error,
							duration_ms: finalState.duration_ms,
							beat_count: finalState.anlz?.beatgrid.beats.length ?? 0
						}
					};
				} finally {
					window.clearInterval(timer);
				}
			},
			{
				incumbentId: incumbent.stable_id,
				candidateAId: candidateA.stable_id,
				candidateBId: candidateB.stable_id,
				moduleUrl: '/src/lib/rb/audio-engine.svelte.ts'
			}
		);
		expect(overlap.outcomes).toEqual(['fulfilled', 'fulfilled']);
		expect(overlap.samples).toContain(incumbent.stable_id);
		expect(overlap.samples).toContain(candidateB.stable_id);
		expect(overlap.samples).not.toContain(candidateA.stable_id);
		expect(overlap.samples).not.toContain(null);
		expect(overlap.final.stable_id).toBe(candidateB.stable_id);
		expect(overlap.final.audible).toBe(false);
		expect(overlap.final.processor_error).toBeNull();
		expect(overlap.final.duration_ms).toBeGreaterThan(0);
		expect(overlap.final.beat_count).toBeGreaterThan(31);
		const ipcState = await _query(page);
		expect(ipcState.decks[4].stable_id).toBe(candidateB.stable_id);

		await _dispatch(page, { type: 'play', deck: 4, playing: true });
		await expect
			.poll(async () => {
				const state = await _query(page);
				return state.decks[4].audible && !state.decks[4].transport_pending;
			})
			.toBe(true);
		const latestCapture = await page.evaluate(() => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) throw new Error('performance IPC is not installed');
			const capture = ipc.capture(4);
			return [...capture.frequency_db, ...capture.time_domain].every((value) =>
				Number.isFinite(value)
			);
		});
		expect(latestCapture).toBe(true);
	});

	test('Master Tempo exposes real processor state through UI and IPC', async ({ page, request }) => {
		const track = (await _fetchAnalyzedTracks(request))[0];
		await _gotoPerformance(page);
		await _dispatch(page, { type: 'load', deck: 1, stable_id: track.stable_id });
		await _dispatch(page, { type: 'play', deck: 1, playing: true });
		let state = await _firstPresentedTransportState(page, 1, true);
		const uiPendingState = _nextTransportPendingState(
			page,
			1,
			state.decks['1'].transport_clock.desired_revision
		);
		await _control(page, 1, 'master-tempo').first().click();
		state = await uiPendingState;
		expect(state.decks['1'].master_tempo_enabled).toBe(false);
		expect(state.decks['1'].transport_pending).toBe(true);
		expect(state.decks['1'].transport_clock.desired_revision).toBeGreaterThan(
			state.decks['1'].transport_clock.presented_revision
		);
		state = await _firstPresentedTransportState(page, 1, true);
		expect(state.decks['1'].master_tempo_enabled).toBe(false);
		await expect(_control(page, 1, 'master-tempo').first()).toHaveAttribute('aria-pressed', 'false');

		state = await _dispatch(page, { type: 'master_tempo', deck: 1, enabled: true });
		expect(state.decks['1'].master_tempo_enabled).toBe(true);
		expect(state.decks['1'].transport_pending).toBe(true);
		expect(state.decks['1'].processor_error).toBeNull();
		state = await _firstPresentedTransportState(page, 1, true);
		expect(state.decks['1'].master_tempo_enabled).toBe(true);
		expect(state.decks['1'].transport_clock.presented_revision).toBe(
			state.decks['1'].transport_clock.desired_revision
		);
		await expect(_control(page, 1, 'master-tempo').first()).toHaveAttribute('aria-pressed', 'true');
		await expect(_control(page, 1, 'master-tempo').first()).toHaveAttribute(
			'data-processor-state',
			'active'
		);
		await _dispatch(page, { type: 'play', deck: 1, playing: false });
		await _firstPresentedTransportState(page, 1, false);
	});

	test('Signalsmith preserves a real PCM tone while tempo advances at 1.10x', async ({ page }, testInfo) => {
		await _gotoPerformance(page);
		const measurements = await page.evaluate(
			async ({ fixtureUrl, moduleUrl }) => {
				const adapter = (await import(moduleUrl)) as StretchAdapterModule;

				const response = await fetch(fixtureUrl, { cache: 'no-store' });
				if (!response.ok) {
					throw new Error(`PCM tone fixture failed with HTTP ${response.status}`);
				}
				const encodedTone = await response.arrayBuffer();

				async function measure(rate: number, semitones: number): Promise<ToneMeasurement> {
					const context = new AudioContext();
					await context.resume();
					const tone = await context.decodeAudioData(encodedTone.slice(0));
					const analyser = context.createAnalyser();
					analyser.fftSize = 16_384;
					analyser.minDecibels = -120;
					analyser.maxDecibels = 0;
					analyser.smoothingTimeConstant = 0;
					const mute = context.createGain();
					mute.gain.value = 0;
					analyser.connect(mute);
					mute.connect(context.destination);

					let processorFailure: Error | null = null;
					const inputSamples: Array<{ context_s: number; input_s: number }> = [];
					const processor = await adapter.StretchDeckProcessor.create(context, {
						onInputTime: (inputTimeSec) => {
							inputSamples.push({ context_s: context.currentTime, input_s: inputTimeSec });
						},
						onProcessorError: (error) => {
							processorFailure = error;
						}
					});
					processor.connect(analyser);
					await processor.load(tone);
					const leadTimeSec = Math.max(0.1, (await processor.latencySec()) + 0.05);
					const scheduledAt = context.currentTime + leadTimeSec;
					await processor.schedule(scheduledAt, {
						active: true,
						input: 0.5,
						rate,
						semitones
					});

					const frequencies: number[] = [];
					const frequencyData = new Float32Array(analyser.frequencyBinCount);
					const binHz = context.sampleRate / analyser.fftSize;
					const firstBin = Math.ceil(100 / binHz);
					const lastBin = Math.floor(1000 / binHz);
					const deadline = performance.now() + 7_000;
					const scheduledInputSamples = () =>
						inputSamples.filter((sample) => sample.context_s >= scheduledAt + 0.02);
					while (
						(frequencies.length < 11 || scheduledInputSamples().length < 5) &&
						performance.now() < deadline
					) {
						await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
						if (processorFailure !== null) throw processorFailure;
						analyser.getFloatFrequencyData(frequencyData);
						let strongestBin = firstBin;
						for (let bin = firstBin + 1; bin <= lastBin; bin++) {
							if (frequencyData[bin] > frequencyData[strongestBin]) strongestBin = bin;
						}
						if (frequencyData[strongestBin] > -70) frequencies.push(strongestBin * binHz);
					}
					if (frequencies.length < 11) {
						throw new Error(`Signalsmith produced only ${frequencies.length} audible snapshots`);
					}
					const tempoSamples = scheduledInputSamples();
					if (tempoSamples.length < 5) {
						throw new Error(`Signalsmith produced only ${tempoSamples.length} scheduled input-time samples`);
					}

					frequencies.sort((left, right) => left - right);
					const firstInput = tempoSamples[0];
					const lastInput = tempoSamples[tempoSamples.length - 1];
					const contextDelta = lastInput.context_s - firstInput.context_s;
					if (contextDelta <= 0) throw new Error(`invalid processor context delta ${contextDelta}`);
					const inputRate = (lastInput.input_s - firstInput.input_s) / contextDelta;
					await processor.stop(context.currentTime + 0.02);
					processor.disconnect();
					await context.close();
					return {
						dominant_hz: frequencies[Math.floor(frequencies.length / 2)],
						input_rate: inputRate
					};
				}

				const rate = 1.1;
				const linkedPitchSemitones = 12 * Math.log2(rate);
				return {
					baseline: await measure(1, 0),
					masterTempoOff: await measure(rate, linkedPitchSemitones),
					masterTempoOn: await measure(rate, 0)
				};
			},
			{
				fixtureUrl: '/__performance_e2e__/tone.wav',
				moduleUrl: '/src/lib/rb/stretch-adapter.ts'
			}
		);
		await testInfo.attach('signalsmith-measurements.json', {
			body: JSON.stringify(measurements, null, 2),
			contentType: 'application/json'
		});

		expect(measurements.masterTempoOff.dominant_hz / measurements.baseline.dominant_hz).toBeGreaterThan(1.07);
		expect(measurements.masterTempoOff.dominant_hz / measurements.baseline.dominant_hz).toBeLessThan(1.13);
		expect(
			Math.abs(
				_centsBetween(
					measurements.masterTempoOn.dominant_hz,
					measurements.baseline.dominant_hz
				)
			)
		).toBeLessThan(35);
		const measuredTempoRatio =
			measurements.masterTempoOn.input_rate / measurements.baseline.input_rate;
		expect(measuredTempoRatio).toBeGreaterThan(1.05);
		expect(measuredTempoRatio).toBeLessThan(1.15);
	});

	test('dispose() retires the real Signalsmith worklet, not a fabricated RPC', async ({ page }) => {
		// tests/unit/stretch-adapter.test.mjs exercises StretchDeckProcessor's OWN
		// dispose() orchestration (idempotency, timeout handling) against a fake
		// node whose dispose() resolves immediately regardless of what a real
		// worklet does - it cannot catch a broken patched-Signalsmith dispose
		// handler. This drives the REAL AudioWorkletNode instead: a worklet that
		// never posts its dispose reply would make this await time out
		// (STRETCH_COMMAND_TIMEOUT_MS) and fail the test.
		await _gotoPerformance(page);
		const result = await page.evaluate(
			async ({ fixtureUrl, moduleUrl }) => {
				const adapter = (await import(moduleUrl)) as StretchAdapterModule;

				const response = await fetch(fixtureUrl, { cache: 'no-store' });
				if (!response.ok) {
					throw new Error(`PCM tone fixture failed with HTTP ${response.status}`);
				}
				const encodedTone = await response.arrayBuffer();

				const context = new AudioContext();
				await context.resume();
				const tone = await context.decodeAudioData(encodedTone.slice(0));
				const processor = await adapter.StretchDeckProcessor.create(context, {
					onInputTime: () => {},
					onProcessorError: () => {}
				});
				processor.connect(context.destination);
				await processor.load(tone);

				await processor.dispose();

				let rejectionName: string | null = null;
				try {
					await processor.schedule(context.currentTime + 0.05, {
						active: true,
						input: 0,
						rate: 1,
						semitones: 0
					});
				} catch (error) {
					rejectionName = error instanceof Error ? error.name : String(error);
				}
				await context.close();
				return { rejectionName };
			},
			{
				fixtureUrl: '/__performance_e2e__/tone.wav',
				moduleUrl: '/src/lib/rb/stretch-adapter.ts'
			}
		);

		expect(
			result.rejectionName,
			'schedule() after a real dispose() must still refuse - the disposed guard must hold ' +
				'against the real worklet path, not just the unit-tested fake'
		).toBe('StretchProcessorError');
	});

	test('a plain play and pause schedule inside the immediate-transport margin', async ({
		page,
		request
	}, testInfo) => {
		// LATENCY-01/03. Beat Sync is OFF on this deck and nothing else is
		// playing, so this is the plain transport path: it must pay the
		// immediate margin, not the 100ms multi-deck alignment margin.
		const track = (await _fetchAnalyzedTracks(request))[0];
		await _gotoPerformance(page);
		await _dispatch(page, { type: 'load', deck: 1, stable_id: track.stable_id });
		await _dispatch(page, { type: 'beat_sync', deck: 1, enabled: false });

		await _dispatch(page, { type: 'play', deck: 1, playing: true });
		await _firstPresentedTransportState(page, 1, true);
		await _dispatch(page, { type: 'play', deck: 1, playing: false });
		await _idleState(page);

		const rows = await _scheduleOffsetRows(page, 1);
		await testInfo.attach('class-a-schedule-offsets.json', {
			body: JSON.stringify(rows, null, 2),
			contentType: 'application/json'
		});

		expect(
			rows.length,
			'if the engine logged no transport-schedule rows then the LATENCY-03 instrument ' +
				'is not wired and this floor is asserting nothing'
		).toBeGreaterThanOrEqual(2);

		for (const row of rows) {
			const stages = row.stages;
			if (stages === undefined) throw new Error('filtered row lost its stages');
			// The part the schedule policy owns. Machine-independent.
			expect(
				stages.safety_ms,
				`plain transport paid ${stages.safety_ms}ms of schedule margin; the beat-sync ` +
					'100ms is back on the click path'
			).toBeLessThanOrEqual(TRANSPORT_SAFETY_CEILING_MS);
			// The whole offset, ratcheting down from a measured 220.0ms.
			expect(
				stages.scheduled_offset_ms,
				`scheduled offset ${stages.scheduled_offset_ms}ms exceeds the ratchet; it was ` +
					'220.0ms before the immediate/sync margin split and 128.0ms before the ' +
					'onset-ramp lead replaced the processor self-report'
			).toBeLessThanOrEqual(TRANSPORT_OFFSET_CEILING_MS);
			// LATENCY round 2: the lead CHARGED must be strictly below what the
			// processor says about itself. Equality means the derivation never
			// fired in the live browser and the offset above passed for some
			// other reason - a smaller block, a different processor - which is
			// exactly the false green a ratchet is prone to.
			expect(
				stages.processor_lead_ms,
				`the floor charged ${stages.processor_lead_ms}ms of processor lead against a ` +
					`${stages.processor_latency_ms}ms self-report; if they are equal then the ` +
					'onset-ramp derivation is not running in the browser'
			).toBeLessThan(stages.processor_latency_ms);
			expect(
				stages.scheduled_offset_ms - stages.processor_lead_ms,
				'the offset must decompose exactly into the processor lead plus the schedule ' +
					'safety, or one of the two logged terms is describing something else'
			).toBeCloseTo(stages.safety_ms, 3);
			// The device floor must travel with the sample, or a number measured
			// here cannot be compared with one measured anywhere else.
			expect(typeof stages.base_latency_ms).toBe('number');
			expect(typeof stages.output_latency_ms).toBe('number');
			expect(typeof stages.processor_latency_ms).toBe('number');
		}
	});

	test('Signalsmith shifts audible pitch when semitones change at a future scheduled output time', async ({
		page
	}, testInfo) => {
		await _gotoPerformance(page);
		const measurement = await page.evaluate(
			async ({ fixtureUrl, moduleUrl }) => {
				const adapter = (await import(moduleUrl)) as StretchAdapterModule;

				const response = await fetch(fixtureUrl, { cache: 'no-store' });
				if (!response.ok) {
					throw new Error(`PCM tone fixture failed with HTTP ${response.status}`);
				}
				const encodedTone = await response.arrayBuffer();

				const context = new AudioContext();
				await context.resume();
				const tone = await context.decodeAudioData(encodedTone.slice(0));
				const analyser = context.createAnalyser();
				analyser.fftSize = 16_384;
				analyser.minDecibels = -120;
				analyser.maxDecibels = 0;
				analyser.smoothingTimeConstant = 0;
				const mute = context.createGain();
				mute.gain.value = 0;
				analyser.connect(mute);
				mute.connect(context.destination);

				let processorFailure: Error | null = null;
				const inputSamples: Array<{ context_s: number; input_s: number }> = [];
				const processor = await adapter.StretchDeckProcessor.create(context, {
					onInputTime: (inputTimeSec) => {
						inputSamples.push({ context_s: context.currentTime, input_s: inputTimeSec });
					},
					onProcessorError: (error) => {
						processorFailure = error;
					}
				});
				processor.connect(analyser);
				await processor.load(tone);

				const leadTimeSec = Math.max(0.1, (await processor.latencySec()) + 0.05);
				const startAt = context.currentTime + leadTimeSec;
				const shiftAt = startAt + 0.5;

				await processor.schedule(startAt, {
					active: true,
					input: 0.5,
					rate: 1.0,
					semitones: 0
				});

				await processor.schedule(shiftAt, {
					active: true,
					input: 1.0,
					rate: 1.0,
					semitones: 1
				});

				const beforeShiftFrequencies: number[] = [];
				const afterShiftFrequencies: number[] = [];
				const frequencyData = new Float32Array(analyser.frequencyBinCount);
				const binHz = context.sampleRate / analyser.fftSize;
				const firstBin = Math.ceil(100 / binHz);
				const lastBin = Math.floor(1000 / binHz);
				const deadline = performance.now() + 7_000;

				while (
					(beforeShiftFrequencies.length < 8 || afterShiftFrequencies.length < 8) &&
					performance.now() < deadline
				) {
					await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
					if (processorFailure !== null) throw processorFailure;
					const now = context.currentTime;
					analyser.getFloatFrequencyData(frequencyData);
					let strongestBin = firstBin;
					for (let bin = firstBin + 1; bin <= lastBin; bin++) {
						if (frequencyData[bin] > frequencyData[strongestBin]) strongestBin = bin;
					}
					if (frequencyData[strongestBin] > -70) {
						const hz = strongestBin * binHz;
						if (now >= startAt + 0.05 && now < shiftAt - 0.02) {
							beforeShiftFrequencies.push(hz);
						} else if (now >= shiftAt + 0.45) {
							afterShiftFrequencies.push(hz);
						}
					}
				}

				await processor.stop(context.currentTime + 0.02);
				processor.disconnect();
				await context.close();

				if (beforeShiftFrequencies.length < 5) {
					throw new Error(`captured only ${beforeShiftFrequencies.length} pre-shift frequency samples`);
				}
				if (afterShiftFrequencies.length < 5) {
					throw new Error(`captured only ${afterShiftFrequencies.length} post-shift frequency samples`);
				}

				beforeShiftFrequencies.sort((a, b) => a - b);
				afterShiftFrequencies.sort((a, b) => a - b);

				const preHz = beforeShiftFrequencies[Math.floor(beforeShiftFrequencies.length / 2)];
				const postHz = afterShiftFrequencies[Math.floor(afterShiftFrequencies.length / 2)];

				return {
					preHz,
					postHz,
					ratio: postHz / preHz
				};
			},
			{
				fixtureUrl: '/__performance_e2e__/tone.wav',
				moduleUrl: '/src/lib/rb/stretch-adapter.ts'
			}
		);

		await testInfo.attach('signalsmith-key-shift-measurement.json', {
			body: JSON.stringify(measurement, null, 2),
			contentType: 'application/json'
		});

		const expectedRatio = 2 ** (1 / 12);
		expect(measurement.ratio).toBeGreaterThan(expectedRatio * 0.95);
		expect(measurement.ratio).toBeLessThan(expectedRatio * 1.05);
	});

	test('KEY SYNC calculation reaches the scheduled Signalsmith tone path', async ({ page }, testInfo) => {
		// This is a production-module DSP check, not a library/UI workflow. Loading
		// the root route keeps the test independent of the real-library server while
		// Vite still serves the exact source modules the performance page imports.
		await page.goto('/');
		const measurement = await page.evaluate(
			async ({ fixtureUrl, adapterModuleUrl, engineModuleUrl }) => {
				const adapter = (await import(adapterModuleUrl)) as StretchAdapterModule;
				const engine = (await import(engineModuleUrl)) as {
					deriveKeySyncTargetManualShift(
						deckKey: string,
						masterKey: string,
						deckEffectiveSemitones: number,
						masterEffectiveSemitones: number,
						deckManualShiftSemitones: number
					): number;
					stretchScheduleChange(
						input: number,
						active: boolean,
						tempoRatio: number,
						masterTempoEnabled: boolean,
						keyShiftSemitones: number,
						loop: null
					): { active: boolean; input: number; rate: number; semitones: number };
				};
				const response = await fetch(fixtureUrl, { cache: 'no-store' });
				if (!response.ok) throw new Error(`PCM tone fixture failed with HTTP ${response.status}`);
				const encodedTone = await response.arrayBuffer();
				const context = new AudioContext();
				await context.resume();
				const tone = await context.decodeAudioData(encodedTone.slice(0));
				const analyser = context.createAnalyser();
				analyser.fftSize = 16_384;
				analyser.minDecibels = -120;
				analyser.maxDecibels = 0;
				analyser.smoothingTimeConstant = 0;
				const mute = context.createGain();
				mute.gain.value = 0;
				analyser.connect(mute);
				mute.connect(context.destination);
				let processorFailure: Error | null = null;
				const processor = await adapter.StretchDeckProcessor.create(context, {
					onInputTime: () => {},
					onProcessorError: (error) => {
						processorFailure = error;
					}
				});
				processor.connect(analyser);
				await processor.load(tone);
				const keySyncShift = engine.deriveKeySyncTargetManualShift('8A', '10A', 0, 0, 0);
				const keySyncChange = engine.stretchScheduleChange(1, true, 1, true, keySyncShift, null);
				const leadTimeSec = Math.max(0.1, (await processor.latencySec()) + 0.05);
				const startAt = context.currentTime + leadTimeSec;
				const shiftAt = startAt + 0.5;
				await processor.schedule(startAt, { active: true, input: 0.5, rate: 1, semitones: 0 });
				await processor.schedule(shiftAt, { ...keySyncChange, input: 1 });
				const before: number[] = [];
				const after: number[] = [];
				const frequencyData = new Float32Array(analyser.frequencyBinCount);
				const binHz = context.sampleRate / analyser.fftSize;
				const firstBin = Math.ceil(100 / binHz);
				const lastBin = Math.floor(1000 / binHz);
				const deadline = performance.now() + 7_000;
				while ((before.length < 8 || after.length < 8) && performance.now() < deadline) {
					await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
					if (processorFailure !== null) throw processorFailure;
					analyser.getFloatFrequencyData(frequencyData);
					let strongestBin = firstBin;
					for (let bin = firstBin + 1; bin <= lastBin; bin++) {
						if (frequencyData[bin] > frequencyData[strongestBin]) strongestBin = bin;
					}
					if (frequencyData[strongestBin] <= -70) continue;
					const hz = strongestBin * binHz;
					if (context.currentTime >= startAt + 0.05 && context.currentTime < shiftAt - 0.02) before.push(hz);
					else if (context.currentTime >= shiftAt + 0.45) after.push(hz);
				}
				await processor.stop(context.currentTime + 0.02);
				processor.disconnect();
				await context.close();
				if (before.length < 5 || after.length < 5) {
					throw new Error(`captured ${before.length} pre-shift and ${after.length} post-shift frequency samples`);
				}
				before.sort((left, right) => left - right);
				after.sort((left, right) => left - right);
				const preHz = before[Math.floor(before.length / 2)];
				const postHz = after[Math.floor(after.length / 2)];
				return { keySyncShift, scheduledSemitones: keySyncChange.semitones, preHz, postHz, ratio: postHz / preHz };
			},
			{
				fixtureUrl: '/__performance_e2e__/tone.wav',
				adapterModuleUrl: '/src/lib/rb/stretch-adapter.ts',
				engineModuleUrl: '/src/lib/rb/audio-engine.svelte.ts'
			}
		);
		await testInfo.attach('signalsmith-key-sync-measurement.json', {
			body: JSON.stringify(measurement, null, 2),
			contentType: 'application/json'
		});
		expect(measurement.keySyncShift).toBe(2);
		expect(measurement.scheduledSemitones).toBe(2);
		const expectedRatio = 2 ** (2 / 12);
		expect(measurement.ratio).toBeGreaterThan(expectedRatio * 0.95);
		expect(measurement.ratio).toBeLessThan(expectedRatio * 1.05);
	});

	test('key nudge buttons shift key on loaded deck and disable at bounds and when unloaded', async ({
		page,
		request
	}) => {
		await _gotoPerformance(page);

		await expect(_control(page, 1, 'key-nudge-up')).toBeDisabled();
		await expect(_control(page, 1, 'key-nudge-down')).toBeDisabled();

		const tracks = await _fetchAnalyzedTracks(request);
		const track = tracks[0];
		await _dispatch(page, { type: 'load', deck: 1, stable_id: track.stable_id });
		await _idleState(page);

		const deck1Header = page.locator('section.rb-deck[data-deck="1"]');
		const keyOff = deck1Header.locator('.key-off');

		await _control(page, 1, 'key-nudge-up').click();
		await _idleState(page);

		let state = await _query(page);
		expect(state.decks[1].key_shift_semitones).toBe(1);
		await expect(keyOff).toHaveText('+1');

		await _control(page, 1, 'key-nudge-down').click();
		await _idleState(page);
		await _control(page, 1, 'key-nudge-down').click();
		await _idleState(page);

		state = await _query(page);
		expect(state.decks[1].key_shift_semitones).toBe(-1);
		await expect(keyOff).toHaveText('-1');

		for (let s = state.decks[1].key_shift_semitones; s < 12; s++) {
			await _dispatch(page, { type: 'key_nudge', deck: 1, semitones: 1 });
		}
		await _idleState(page);
		state = await _query(page);
		expect(state.decks[1].key_shift_semitones).toBe(12);
		await expect(_control(page, 1, 'key-nudge-up')).toBeDisabled();
		await expect(_control(page, 1, 'key-nudge-down')).toBeEnabled();

		for (let s = 12; s > -12; s--) {
			await _dispatch(page, { type: 'key_nudge', deck: 1, semitones: -1 });
		}
		await _idleState(page);
		state = await _query(page);
		expect(state.decks[1].key_shift_semitones).toBe(-12);
		await expect(_control(page, 1, 'key-nudge-down')).toBeDisabled();
		await expect(_control(page, 1, 'key-nudge-up')).toBeEnabled();
	});

	// DECKUX-04: the unit suite proves the two painters agree. This checks
	// that the real page wires both surfaces to
	// the engaged loop, which is the half a pure-function test cannot reach.
	test('an engaged loop repaints EVERY deck waveform surface, not just the primary', async ({
		page,
		request
	}, testInfo) => {
		const track = (await _fetchAnalyzedTracks(request))[0];
		await page.setViewportSize({ width: 1280, height: 800 });
		await _gotoPerformance(page);
		await _dispatch(page, { type: 'load', deck: 1, stable_id: track.stable_id });
		await _idleState(page);

		// A loop wide enough to be unmissable on the strip, which squeezes the
		// whole track into ~400px: 16 beats, placed away from the track head.
		const loopIn = track.beatgrid_ms[12];
		const loopOut = track.beatgrid_ms[28];
		expect(loopOut).toBeGreaterThan(loopIn);

		await _recordSurfaceBaseline(page);
		const state = await _dispatch(page, {
			type: 'loop',
			deck: 1,
			loop: { in_ms: loopIn, out_ms: loopOut }
		});
		expect(state.decks[1].loop).toEqual({
			in_ms: loopIn,
			out_ms: loopOut,
			engaged: true,
			beat_length: null
		});

		// Both surfaces repaint on their own schedule, so poll until the slowest
		// has painted rather than racing it with a fixed wait.
		await expect
			.poll(async () => (await _surfaceRepaintCounts(page)).every((s) => s.changed > 0), {
				message: 'every deck waveform surface must repaint once a loop is engaged'
			})
			.toBe(true);

		const counts = await _surfaceRepaintCounts(page);
		await testInfo.attach('deckux-04-loop-repaint-counts.json', {
			body: JSON.stringify({ loop_in_ms: loopIn, loop_out_ms: loopOut, counts }, null, 2),
			contentType: 'application/json'
		});
		const loopShotPath = testInfo.outputPath('deckux-04-loop-on-every-surface.png');
		await page.screenshot({ path: loopShotPath, fullPage: false });
		await testInfo.attach('deckux-04-loop-on-every-surface.png', {
			path: loopShotPath,
			contentType: 'image/png'
		});
		for (const surface of counts) {
			expect(
				surface.changed,
				`${surface.name} painted no loop: ${surface.changed} of ${surface.sampled} pixels changed`
			).toBeGreaterThan(0);
		}

		// Disengaging must clear it everywhere too, so a stale band cannot lie
		// about a loop that is no longer running.
		await _recordSurfaceBaseline(page);
		await _dispatch(page, { type: 'loop', deck: 1, loop: null });
		await expect
			.poll(async () => (await _surfaceRepaintCounts(page)).every((s) => s.changed > 0), {
				message: 'every deck waveform surface must repaint once the loop is cleared'
			})
			.toBe(true);
	});
});
