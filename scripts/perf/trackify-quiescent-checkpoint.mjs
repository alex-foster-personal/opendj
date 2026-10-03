/**
 * Quiescent checkpoints for the PERFMODE-15 Trackify leak capture
 * (ADR-NEW-trackify-leak-kpi-quiescent-baselines).
 *
 * The leak KPI is the slope of footprint baselines taken with the workload
 * undone: autoplay off, deck unloaded, garbage collected. What is still
 * resident then has outlived the track that needed it. The footprint while a
 * track plays also carries that track's decoded audio, which follows track
 * length, so a slope over playing samples measures playlist order.
 *
 * Side-effect-free and importable without a browser, like
 * `trackify-playback-watch.mjs`, so the protocol is unit tested against a fake
 * page. `page` is the `uninstrumented-page.mjs` handle: `evaluate`,
 * `waitForFunction`, `waitForTimeout` and raw `send`.
 */
import { createInterface } from 'node:readline';

/** Settle after the unload before collecting, so teardown work has run. */
export const QUIESCE_SETTLE_MS = 3_000;
/** Settle after the critical memory-pressure notification. */
export const PRESSURE_SETTLE_MS = 2_000;
const UNLOAD_TIMEOUT_MS = 30_000;
const RESUME_TIMEOUT_MS = 60_000;

/** Two full collections: the second one reclaims what the first one's finalizers released. */
async function _collectGarbage(page) {
	await page.send('HeapProfiler.collectGarbage');
	await page.send('HeapProfiler.collectGarbage');
}

async function _trackifyDeck(page) {
	return page.evaluate(() => {
		const ipc = window.musicDjToolsTrackify;
		if (ipc === undefined) throw new Error('Trackify IPC is not installed');
		const deck = ipc.query().deck;
		return { stable_id: deck.stable_id, playing: deck.playing };
	});
}

/**
 * Stops Trackify's workload and collects garbage. Resolves only with the deck
 * empty both before and after collection; throws otherwise, so a baseline is
 * never sampled over a loaded track.
 */
export async function quiesceTrackify(
	page,
	{ settleMs = QUIESCE_SETTLE_MS, pressureSettleMs = PRESSURE_SETTLE_MS } = {}
) {
	// Bounded (Codex P1 r4171164376, PR #4888): turning autoplay off does not
	// cancel a deck-1 load already in flight, and the unload queues behind it.
	// If that load never settles, the unload must fail the checkpoint by name
	// rather than hold the capture forever without a QUIESCENT.
	await page.evaluate((timeoutMs) => {
		const trackify = window.musicDjToolsTrackify;
		const performance = window.musicDjToolsPerformance;
		if (trackify === undefined) throw new Error('Trackify IPC is not installed');
		if (performance === undefined) throw new Error('Performance IPC is not installed');
		trackify.toggle_autoplay(false);
		let timer;
		const refusal = new Promise((_, reject) => {
			timer = setTimeout(
				() => reject(new Error(`quiescent checkpoint invalid: deck 1 unload did not settle within ${timeoutMs} ms`)),
				timeoutMs
			);
		});
		return Promise.race([performance.dispatch({ type: 'unload', deck: 1 }), refusal]).finally(() =>
			clearTimeout(timer)
		);
	}, UNLOAD_TIMEOUT_MS);
	await page.waitForFunction(
		() => window.musicDjToolsTrackify?.query().deck.stable_id === null,
		undefined,
		{ timeout: UNLOAD_TIMEOUT_MS }
	);
	await page.waitForTimeout(settleMs);
	await _collectGarbage(page);
	await page.send('Memory.simulatePressureNotification', { level: 'critical' });
	await page.waitForTimeout(pressureSettleMs);
	await _collectGarbage(page);
	const deck = await _trackifyDeck(page);
	if (deck.stable_id !== null) {
		throw new Error(
			`quiescent checkpoint invalid: deck 1 reloaded ${deck.stable_id} while autoplay was off, ` +
				'so the baseline would include a loaded track'
		);
	}
}

/** Turns autoplay back on and resolves once a track is loaded and playing. */
export async function resumeTrackify(page) {
	await page.evaluate(() => {
		const trackify = window.musicDjToolsTrackify;
		if (trackify === undefined) throw new Error('Trackify IPC is not installed');
		trackify.toggle_autoplay(true);
	});
	await page.waitForFunction(
		() => {
			const state = window.musicDjToolsTrackify?.query();
			return state !== undefined && state.deck.stable_id !== null && state.deck.playing === true;
		},
		undefined,
		{ timeout: RESUME_TIMEOUT_MS }
	);
}

/**
 * The leak capture's stdin/stdout protocol, after TRACKIFY_READY:
 * `CHECKPOINT` -> quiesce, print `QUIESCENT`, wait for `RESUME` -> resume,
 * print `RESUMED`; `NEXT` ends it. `watch(page, linePromise)` polls playback
 * until the next line arrives, so every segment between checkpoints is still
 * held to the continuous-playback rules.
 */
export async function runLeakProtocol(page, nextLine, emit, watch, quiesceOptions = {}) {
	for (;;) {
		const line = nextLine();
		await watch(page, line);
		const command = await line;
		if (command === 'NEXT') return;
		if (command !== 'CHECKPOINT') {
			throw new Error(`leak protocol expected CHECKPOINT or NEXT, got ${JSON.stringify(command)}`);
		}
		await quiesceTrackify(page, quiesceOptions);
		emit('QUIESCENT');
		const resume = await nextLine();
		if (resume !== 'RESUME') {
			throw new Error(`leak protocol expected RESUME after QUIESCENT, got ${JSON.stringify(resume)}`);
		}
		await resumeTrackify(page);
		emit('RESUMED');
	}
}

/**
 * One reader over a stream for the whole session: `next()` resolves with each
 * line in order, and rejects once the stream ends with no line left.
 */
export function createLineReader(input) {
	const lines = [];
	const waiters = [];
	let ended = false;
	const rl = createInterface({ input });
	rl.on('line', (line) => {
		const waiter = waiters.shift();
		if (waiter === undefined) lines.push(line);
		else waiter.resolve(line);
	});
	rl.on('close', () => {
		ended = true;
		for (const waiter of waiters.splice(0)) waiter.reject(new Error('stdin closed before the next protocol line'));
	});
	return {
		next() {
			if (lines.length > 0) return Promise.resolve(lines.shift());
			if (ended) return Promise.reject(new Error('stdin closed before the next protocol line'));
			return new Promise((resolve, reject) => waiters.push({ resolve, reject }));
		},
		close() {
			rl.close();
		}
	};
}
