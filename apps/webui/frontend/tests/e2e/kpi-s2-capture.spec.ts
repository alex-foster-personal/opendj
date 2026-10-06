import { test } from '@playwright/test';
import { writeFileSync } from 'node:fs';

import {
	type KpiS2CaptureResult,
	S2_PRESS_KIND,
	type S2PressRow
} from '../../src/lib/perf/kpi-s2-capture-types';

const RESULT_PATH = process.env.KPI_CAPTURE_RESULT;
const ENGINE_ORIGIN = (
	process.env.KPI_CAPTURE_ENGINE ??
	process.env.PERFORMANCE_E2E_API_BASE ??
	'http://127.0.0.1:8686'
).replace(/\/$/, '');
const REQUESTED_PRESSES = Number.parseInt(process.env.KPI_CAPTURE_PRESSES ?? '32', 10);
const TRACK_OVERRIDE = process.env.KPI_CAPTURE_TRACK;
const CAPTURE_BROWSER = (process.env.KPI_CAPTURE_BROWSER ?? 'webkit').toLowerCase();
const MASTER_VOLUME = 0.1;
const DECK = 1 as const;

function writeResult(result: KpiS2CaptureResult): void {
	if (!RESULT_PATH) {
		throw new Error('KPI_CAPTURE_RESULT is required');
	}
	writeFileSync(RESULT_PATH, JSON.stringify(result), 'utf-8');
}

function isCompletePress(row: S2PressRow): boolean {
	if (row.kind !== S2_PRESS_KIND) return false;
	const stages = row.stages;
	if (stages === undefined) return false;
	const base = stages.base_latency_ms;
	const output = stages.output_latency_ms;
	const total = stages.input_to_output_ms;
	if (typeof base !== 'number' || base <= 0) return false;
	if (typeof output !== 'number' || output <= 0) return false;
	if (typeof total !== 'number' || total <= 0) return false;
	if (row.labels?.latency_floor !== 'complete') return false;
	return true;
}

test('capture S2 press-to-audible press rows', async ({ page, request, browser }) => {
	if (!RESULT_PATH) {
		throw new Error('KPI_CAPTURE_RESULT is required');
	}

	const engineLabel =
		CAPTURE_BROWSER === 'webkit' ? `WebKit ${browser.version()}` : `Chromium ${browser.version()}`;

	const fail = (reason: string, floor?: Record<string, unknown>) => {
		writeResult({
			ok: false,
			engine: engineLabel,
			browser: CAPTURE_BROWSER,
			presses: [],
			reason,
			...(floor !== undefined ? { floor } : {})
		});
	};

	await page.goto('/performance');
	try {
		await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, {
			timeout: 30_000
		});
	} catch {
		fail('performance IPC is not installed');
		return;
	}

	await page.evaluate(async (volume) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		await ipc.dispatch({ type: 'master_volume', value: volume });
	}, MASTER_VOLUME);

	let stableId = TRACK_OVERRIDE ?? null;
	if (stableId === null) {
		const tracksResponse = await request.get(`${ENGINE_ORIGIN}/api/v1/tracks?limit=1000&available=true`);
		if (!tracksResponse.ok()) {
			fail(`tracks API returned HTTP ${tracksResponse.status()}`);
			return;
		}
		const payload = (await tracksResponse.json()) as {
			items?: Array<{ stable_id?: string; file_exists?: boolean }>;
		};
		const items = Array.isArray(payload.items) ? payload.items : [];
		const present = items.find((item) => item.file_exists !== false && typeof item.stable_id === 'string');
		stableId = present?.stable_id ?? null;
	}
	if (stableId === null) {
		fail('no present track to load');
		return;
	}

	await page.evaluate(
		async ({ deck, trackId }) => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) throw new Error('performance IPC is not installed');
			await ipc.dispatch({ type: 'load', deck, stable_id: trackId });
		},
		{ deck: DECK, trackId: stableId }
	);

	await page.waitForFunction(
		(deck) => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) return false;
			const state = ipc.query().decks[deck];
			return state.stable_id !== null && state.transport_pending === false;
		},
		DECK,
		{ timeout: 60_000 }
	);

	await page.evaluate(async (deck) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		await ipc.dispatch({ type: 'beat_sync', deck, enabled: false });
		await ipc.dispatch({ type: 'quantize', deck, enabled: false, by_user: true });
	}, DECK);

	const syncOff = await page.evaluate((deck) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) return false;
		const state = ipc.query().decks[deck];
		return state.beat_sync_enabled === false && state.quantize_enabled === false;
	}, DECK);
	if (!syncOff) {
		fail('autosync still enabled');
		return;
	}

	await page.evaluate(async (deck) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		const t0 = performance.now();
		await ipc.dispatch({ type: 'play', deck, playing: true }, t0);
	}, DECK);

	await page.waitForTimeout(600);

	const primed = await page.waitForFunction(
		() => {
			const read = (window as Window & { __mdtPerfLog?: () => readonly S2PressRow[] }).__mdtPerfLog;
			if (read === undefined) return false;
			return read().some((row) => {
				if (row.kind !== S2_PRESS_KIND) return false;
				const stages = row.stages;
				if (stages === undefined) return false;
				const base = stages.base_latency_ms;
				const output = stages.output_latency_ms;
				const total = stages.input_to_output_ms;
				if (typeof base !== 'number' || base <= 0) return false;
				if (typeof output !== 'number' || output <= 0) return false;
				if (typeof total !== 'number' || total <= 0) return false;
				if (row.labels?.latency_floor !== 'complete') return false;
				return true;
			});
		},
		undefined,
		{ timeout: 30_000 }
	).catch(() => null);
	if (primed === null) {
		const floor = await page.evaluate(() => {
			const read = (window as Window & { __mdtPerfLog?: () => readonly S2PressRow[] }).__mdtPerfLog;
			const rows = read?.() ?? [];
			const last = rows.findLast((row) => row.kind.includes(S2_PRESS_KIND));
			return {
				labels: last?.labels ?? null,
				stages: last?.stages ?? null
			};
		});
		fail('audio context had not rendered or outputLatency is 0', floor);
		return;
	}

	await page.evaluate(async (deck) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		const t0 = performance.now();
		await ipc.dispatch({ type: 'play', deck, playing: false }, t0);
	}, DECK);

	await page.waitForFunction(
		(deck) => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) return false;
			const state = ipc.query().decks[deck];
			return state.transport_pending === false && state.playing === false;
		},
		DECK,
		{ timeout: 15_000 }
	);

	const collected: S2PressRow[] = [];
	const seen = new Set<string>();
	const rowKey = (row: S2PressRow) =>
		JSON.stringify({
			kind: row.kind,
			deck: row.deck,
			stages: row.stages,
			labels: row.labels
		});

	// Prime play/pause rows stay in the ring; mark them seen so they are not scored.
	const preLoopRows = await page.evaluate(() => {
		const read = (window as Window & { __mdtPerfLog?: () => readonly S2PressRow[] }).__mdtPerfLog;
		if (read === undefined) return [];
		return read().filter((row) => row.kind === S2_PRESS_KIND) as S2PressRow[];
	});
	for (const row of preLoopRows) {
		seen.add(rowKey(row));
	}

	let playing = false;
	for (let index = 0; index < REQUESTED_PRESSES; index += 1) {
		playing = !playing;
		await page.evaluate(
			async ({ deck, nextPlaying }) => {
				const ipc = window.musicDjToolsPerformance;
				if (ipc === undefined) throw new Error('performance IPC is not installed');
				const t0 = performance.now();
				await ipc.dispatch({ type: 'play', deck, playing: nextPlaying }, t0);
			},
			{ deck: DECK, nextPlaying: playing }
		);

		await page.waitForFunction(
			({ deck, expectedPlaying }) => {
				const ipc = window.musicDjToolsPerformance;
				if (ipc === undefined) return false;
				const state = ipc.query().decks[deck];
				return state.transport_pending === false && state.playing === expectedPlaying;
			},
			{ deck: DECK, expectedPlaying: playing },
			{ timeout: 15_000 }
		);

		const newRows = await page.evaluate(() => {
			const read = (window as Window & { __mdtPerfLog?: () => readonly S2PressRow[] }).__mdtPerfLog;
			if (read === undefined) throw new Error('__mdtPerfLog is not installed');
			return read().filter((row) => row.kind === S2_PRESS_KIND) as S2PressRow[];
		});
		for (const row of newRows) {
			const key = rowKey(row);
			if (!seen.has(key)) {
				seen.add(key);
				collected.push(row);
			}
		}

		await page.waitForTimeout(75);
	}

	const complete = collected.filter((row) => isCompletePress(row));
	if (complete.length < REQUESTED_PRESSES) {
		fail(
			`fewer than ${REQUESTED_PRESSES} complete-floor ordinary presses (got ${complete.length})`
		);
		return;
	}

	writeResult({
		ok: true,
		engine: engineLabel,
		browser: CAPTURE_BROWSER,
		presses: complete.slice(0, REQUESTED_PRESSES),
		reason: null
	});
});
