/**
 * Scorer for "stems land on a deck while another deck plays" (PERF-STEMLOAD).
 *
 * Runs INSIDE an open /performance page served by the vite dev server, against
 * the real engine module and a real AudioContext. Nothing is mocked. Load it
 * from the page console (or an agent's page-eval tool):
 *
 *   const s = await import('/@fs/<abs path to this file>');   (the dev server
 *     must allow the path: server.fs.allow)
 *   await s.runCondition({ label: 'local+playing', loadDeck: 2, sids: [...] });
 *   s.summarize(window.__stemLoadRuns)
 *
 * The caller owns the preconditions (master muted, which deck is playing). The
 * scorer refuses to run unmuted, because it loads real tracks.
 *
 * Per run it reports:
 *   ready_ms            load call to `stems.status === 'ready'`
 *   underrun_events     AudioContext.playbackStats delta. NOT EVIDENCE: on
 *                       Chromium 152 it stayed 0 through deliberate 15 to
 *                       120 ms audio-thread stalls that the sentinel caught
 *                       every time (Thu 1 Oct 2026), so its zero says nothing.
 *                       Kept so a build where it works shows up as nonzero.
 *   sentinel_xruns      late render callbacks seen by a dedicated sentinel
 *                       worklet reporting every 100 ms, with the worst gap
 *   app_xruns           the app's own always-on sentinel counter delta
 *   longtasks           main-thread tasks over 50 ms (count, total, max).
 *                       Null unless the observer passed its own positive
 *                       control in this page (a deliberate 120 ms block that
 *                       it must report), see longtask_instrument
 *   late_above_background   in summarize(): late callbacks minus what the
 *                       idle windows of the SAME condition predict for the
 *                       same wall time (runCondition measures one idle window
 *                       before every load)
 *   heap_peak_mb        peak performance.memory.usedJSHeapSize (Chromium only;
 *                       decoded PCM lives outside it, see pcm_mb)
 * Every dropout and long task is attributed to the stem-load phase it fell in.
 *
 * A measurement that could not be taken is reported as null, never as zero.
 */

const ENGINE_MODULE = '/src/lib/rb/audio-engine.svelte.ts';
const XRUN_MATH_MODULE = '/src/lib/rb/xrun-math.ts';
const SENTINEL_REPORT_MS = 100;
const POLL_MS = 10;

let _probe = null;

function _sleep(ms) {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

/** The page's OWN module instance. The dev server rewrites every `import()`
 * it can see in a served file to a `?import` URL, which is a different module
 * URL, so the engine would be evaluated a second time. An importer built at
 * run time is not rewritten. */
const _nativeImport = new Function('url', 'return import(url)');

function _pageModule(path) {
	return _nativeImport(new URL(path, location.origin).href);
}

// -------------------------------------------------------- long-task instrument

const LONGTASK_CONTROL_BLOCK_MS = 120;
let _longTasks = null;

/**
 * One page-lifetime long-task observer, armed once and proven able to report:
 * it must see a deliberate block before any zero it returns is believed. A
 * per-run observer that is disconnected at the end of the run drops whatever
 * is still queued, and an observer that never fires reads as "no long tasks".
 */
async function _armLongTasks() {
	if (_longTasks !== null) return _longTasks;
	const state = { entries: [], observer: null, status: 'unsupported', control_seen_ms: null };
	_longTasks = state;
	if (typeof PerformanceObserver !== 'function' || !PerformanceObserver.supportedEntryTypes?.includes('longtask')) {
		return state;
	}
	const take = (list) => {
		for (const entry of list) state.entries.push({ t: entry.startTime, dur: entry.duration });
	};
	state.observer = new PerformanceObserver((list) => take(list.getEntries()));
	state.observer.observe({ type: 'longtask' });
	state.drain = () => take(state.observer.takeRecords());
	await _sleep(50);
	const blockStart = performance.now();
	while (performance.now() - blockStart < LONGTASK_CONTROL_BLOCK_MS) { /* deliberate block */ }
	const blockEnd = performance.now();
	for (let waited = 0; waited < 3000; waited += 100) {
		await _sleep(100);
		state.drain();
		const hit = state.entries.find((entry) => entry.t <= blockEnd && entry.t + entry.dur >= blockStart + 50);
		if (hit !== undefined) {
			state.status = 'verified';
			state.control_seen_ms = Math.round(hit.dur);
			state.control_window = [blockStart, blockEnd];
			return state;
		}
	}
	state.status = 'control-failed';
	return state;
}

function _longTasksBetween(state, t0, t1) {
	if (state.status !== 'verified') return null;
	state.drain();
	const [c0, c1] = state.control_window;
	return state.entries.filter((task) =>
		task.t + task.dur >= t0 && task.t <= t1 && !(task.t <= c1 && task.t + task.dur >= c0));
}

async function _engine() {
	return _pageModule(ENGINE_MODULE);
}

function _context(engineModule) {
	const node = engineModule.masterDelayNode();
	if (node === null) throw new Error('scorer: the audio graph is not built (load a deck first)');
	return node.context;
}

/** One extra sentinel node, reporting every 100 ms so a dropout can be placed
 * on the load timeline. The app's own sentinel reports every 2 s. */
async function _armProbe(ctx) {
	if (_probe !== null && _probe.ctx === ctx) return _probe;
	const math = await _pageModule(XRUN_MATH_MODULE);
	const quantumMs = Math.max(
		ctx.baseLatency * 1000,
		math.quantumDurationMs(math.RENDER_QUANTUM_FRAMES, ctx.sampleRate)
	);
	const node = new AudioWorkletNode(ctx, 'mdt-xrun-sentinel', {
		numberOfInputs: 0,
		numberOfOutputs: 1,
		outputChannelCount: [1],
		processorOptions: {
			thresholdMs: math.xrunThresholdFromCadenceMs([quantumMs]),
			parkedGapMs: math.XRUN_PARKED_GAP_MS,
			reportIntervalMs: SENTINEL_REPORT_MS,
			cadenceQuantile: math.XRUN_CADENCE_QUANTILE,
			cadenceWindow: math.XRUN_CADENCE_WINDOW,
			warmupCallbacks: math.XRUN_CADENCE_WARMUP_CALLBACKS,
			gapFactor: math.XRUN_GAP_FACTOR,
			gapFloorMs: math.XRUN_GAP_FLOOR_MS
		}
	});
	const silence = ctx.createGain();
	silence.gain.value = 0;
	node.connect(silence);
	silence.connect(ctx.destination);
	const probe = { ctx, node, reports: [], thresholdMs: null };
	node.port.onmessage = (event) => {
		const data = event.data;
		if (typeof data?.xruns !== 'number') return;
		probe.thresholdMs = data.threshold_ms;
		probe.reports.push({
			t: performance.now(),
			xruns: data.xruns,
			worst: data.worst_gap_ms,
			callbacks: data.callbacks
		});
		if (probe.reports.length > 20000) probe.reports.splice(0, 10000);
	};
	_probe = probe;
	// Let the sentinel measure the device cadence before anything is judged.
	await _sleep(2500);
	return probe;
}

/**
 * Audio-thread render load, measured from inside the render loop.
 *
 * The device asks for several 128-frame quanta back to back, so within one
 * device callback the gap between two consecutive `process()` calls of the
 * same node IS the time the whole graph took to render one quantum. The gap
 * to the next device callback is the rest of the period. Pairing consecutive
 * gaps and taking the smaller one therefore reads the per-quantum render time
 * whenever the device buffer holds two or more quanta and load is under half.
 * `Date.now()` is the only clock in a worklet scope and is 1 ms coarse, so a
 * single reading is quantized; the SUM over a window is not biased.
 *
 * Reported per window: callbacks, total wall time, the summed "short" gaps
 * and the largest gap. load = short_ms / wall_ms * (quanta per device callback).
 */
const LOAD_PROBE_SOURCE = `
class StemScorerLoadProbe extends AudioWorkletProcessor {
	constructor() {
		super();
		this.last = null; this.prev = null; this.n = 0; this.short = 0; this.wall = 0; this.max = 0; this.start = null;
	}
	process() {
		const now = Date.now();
		if (this.last === null) { this.last = now; this.start = now; return true; }
		const gap = now - this.last;
		this.last = now;
		this.n += 1;
		this.wall += gap;
		if (gap > this.max) this.max = gap;
		if (this.prev === null) { this.prev = gap; }
		else { this.short += Math.min(this.prev, gap); this.prev = null; }
		if (now - this.start >= 100) {
			this.port.postMessage({ n: this.n, short: this.short, wall: this.wall, max: this.max });
			this.n = 0; this.short = 0; this.wall = 0; this.max = 0; this.start = now;
		}
		return true;
	}
}
registerProcessor('stem-scorer-load-probe', StemScorerLoadProbe);
`;

let _loadProbe = null;

async function _armLoadProbe(ctx) {
	if (_loadProbe !== null && _loadProbe.ctx === ctx) return _loadProbe;
	const url = URL.createObjectURL(new Blob([LOAD_PROBE_SOURCE], { type: 'text/javascript' }));
	await ctx.audioWorklet.addModule(url);
	const node = new AudioWorkletNode(ctx, 'stem-scorer-load-probe', {
		numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1]
	});
	const silence = ctx.createGain();
	silence.gain.value = 0;
	node.connect(silence);
	silence.connect(ctx.destination);
	const probe = { ctx, node, windows: [] };
	node.port.onmessage = (event) => {
		probe.windows.push({ t: performance.now(), ...event.data });
		if (probe.windows.length > 20000) probe.windows.splice(0, 10000);
	};
	_loadProbe = probe;
	return probe;
}

/** Render load over the windows since `fromIndex`: the share of each device
 * period spent rendering. null when the device buffer is a single quantum
 * (there is then no back-to-back pair to read). */
function _renderLoad(probe, fromIndex, sinceMs) {
	const windows = probe.windows.slice(fromIndex).filter((w) => w.t >= sinceMs);
	const wall = windows.reduce((sum, w) => sum + w.wall, 0);
	const short = windows.reduce((sum, w) => sum + w.short, 0);
	const callbacks = windows.reduce((sum, w) => sum + w.n, 0);
	if (wall <= 0 || callbacks === 0) return null;
	const quantumMs = (128 / probe.ctx.sampleRate) * 1000;
	// short/wall is the share of each callback pair spent in its back-to-back
	// half: per-pair render time over per-pair wall time.
	const perWindow = windows.map((w) => (w.wall > 0 ? w.short / w.wall : 0));
	return {
		mean: +(short / wall).toFixed(3),
		peak_window: +Math.max(...perWindow).toFixed(3),
		max_gap_ms: Math.max(...windows.map((w) => w.max)),
		realtime_ratio: +((callbacks * quantumMs) / wall).toFixed(3)
	};
}

function _phaseAt(transitions, t) {
	let name = 'before-load';
	for (const [phase, at] of transitions) {
		if (at <= t) name = phase;
		else break;
	}
	return name;
}

function _stemPhase(stems) {
	if (stems.status === 'loading') return `loading:${stems.load?.phase ?? '?'}`;
	return stems.status;
}

function _playbackStats(ctx) {
	const stats = ctx.playbackStats;
	if (stats === undefined || stats === null) return null;
	return { events: stats.underrunEvents, ms: stats.underrunDuration * 1000 };
}

function _clockSteps(samples, t0) {
	const rates = [];
	for (let i = 1; i < samples.length; i += 1) {
		const dt = samples[i][0] - samples[i - 1][0];
		if (dt > 0) rates.push((samples[i][1] - samples[i - 1][1]) / dt);
	}
	const rate = _median(rates);
	if (rate === null) return null;
	const steps = [];
	for (let i = 1; i < samples.length; i += 1) {
		const excess = (samples[i][1] - samples[i - 1][1]) - rate * (samples[i][0] - samples[i - 1][0]);
		if (Math.abs(excess) > 40) steps.push({ at_ms: Math.round(samples[i][0] - t0), step_ms: Math.round(excess) });
	}
	return { rate: +rate.toFixed(4), samples: samples.length, steps };
}

function _handoffRows() {
	return window.__mdtPerfLog().filter((e) => e.kind === 'stem-live-handoff');
}

function _heapMb() {
	const memory = performance.memory;
	return memory === undefined ? null : memory.usedJSHeapSize / 1048576;
}

/** One measured stem load. Resolves with the run record; never throws for a
 * load that fails, it records the failure instead. */
export async function runStemLoad({ loadDeck, sid, label, settleMs = 2500, timeoutMs = 120000, playWhenMixReady = false }) {
	if (typeof loadDeck !== 'number') throw new TypeError('scorer: loadDeck must be a number (1..4)');
	const m = await _engine();
	if (!m.isMasterMuted()) throw new Error('scorer: master must be muted before loading tracks');
	const ctx = _context(m);
	const probe = await _armProbe(ctx);
	const loadProbe = await _armLoadProbe(ctx);
	// Deck ids are NUMBERS (1..4); the engine rejects the string form.
	const playing = Object.keys(m.deckStates).map(Number).filter((deck) => m.deckStates[deck].playing);
	const playingPosBefore = Object.fromEntries(playing.map((d) => [d, m.deckAudioClockPositionMs(d)]));

	const longTaskState = await _armLongTasks();
	const handoffRowsBefore = _handoffRows();

	const appXrunsBefore = await window.__mdtFlushXruns();
	const statsBefore = _playbackStats(ctx);
	const heapBase = _heapMb();
	let heapPeak = heapBase;
	const underrunTimeline = [];
	let lastStats = statsBefore;
	const probeFrom = probe.reports.length;
	const loadFrom = loadProbe.windows.length;

	const transitions = [];
	const visibilityAtStart = document.visibilityState;
	let polls = 0;
	const t0 = performance.now();
	transitions.push(['load', t0]);
	let loadError = null;
	let loadResolvedAt = null;
	const loading = m.engine
		.load(loadDeck, sid)
		.then(() => { loadResolvedAt = performance.now(); })
		.catch((error) => { loadError = String(error); loadResolvedAt = performance.now(); });

	let lastPhase = '';
	let readyAt = null;
	let terminal = null;
	let lastSample = 0;
	// The live handoff: the load deck starts playing its mix the moment the mix
	// is ready, so the stems arrive on a deck that is itself playing.
	let playAt = null;
	let playError = null;
	let posAtPlay = null;
	const positionSamples = [];
	while (performance.now() - t0 < timeoutMs) {
		const now = performance.now();
		const stems = m.deckStates[loadDeck].stems;
		if (playWhenMixReady && playAt === null && loadResolvedAt !== null && loadError === null) {
			playAt = now;
			posAtPlay = m.deckAudioClockPositionMs(loadDeck);
			m.engine.play(loadDeck).catch((error) => { playError = String(error); });
		}
		if (playAt !== null) positionSamples.push([now, m.deckAudioClockPositionMs(loadDeck)]);
		const phase = loadResolvedAt === null ? 'mix-load' : _stemPhase(stems);
		if (readyAt === null && phase !== lastPhase && phase !== 'ready') {
			transitions.push([phase, now]);
			lastPhase = phase;
		}
		if (now - lastSample >= SENTINEL_REPORT_MS) {
			lastSample = now;
			const heap = _heapMb();
			if (heap !== null && heap > heapPeak) heapPeak = heap;
			const stats = _playbackStats(ctx);
			if (stats !== null && lastStats !== null && stats.events > lastStats.events) {
				underrunTimeline.push({ t: now, events: stats.events - lastStats.events, ms: stats.ms - lastStats.ms });
			}
			lastStats = stats;
		}
		if (loadError !== null) { terminal = 'load-error'; break; }
		if (loadResolvedAt !== null && readyAt === null && stems.status === 'ready') {
			readyAt = now;
			transitions.push(['settle', now]);
			lastPhase = 'settle';
		}
		if (loadResolvedAt !== null && readyAt === null && (stems.status === 'unavailable' || stems.status === 'error')) {
			terminal = `stems-${stems.status}: ${stems.error ?? ''}`;
			break;
		}
		if (readyAt !== null && now - readyAt >= settleMs) { terminal = 'ready'; break; }
		polls += 1;
		await _sleep(POLL_MS);
	}
	if (terminal === null) terminal = 'timeout';
	await loading;
	const tEnd = performance.now();
	// Let queued long-task entries reach the observer before they are read.
	await _sleep(150);

	const appXrunsAfter = await window.__mdtFlushXruns();
	const statsAfter = _playbackStats(ctx);
	const probeReports = probe.reports.slice(probeFrom).filter((r) => r.t >= t0);
	const sentinelEvents = probeReports.filter((r) => r.xruns > 0);
	const stageRow = window.__mdtPerfLog().filter((e) =>
		typeof e.kind === 'string' && e.kind.startsWith('deck-stems') && e.kind.includes(sid.slice(0, 12))).pop() ?? null;
	const loadRow = window.__mdtPerfLog().filter((e) =>
		typeof e.kind === 'string' && e.kind.startsWith('deck-load sid=') && e.kind.includes(sid.slice(0, 12))).pop() ?? null;
	const inRunOrNull = _longTasksBetween(longTaskState, t0, tEnd);
	const inRun = inRunOrNull ?? [];
	const handoffRowsAfter = _handoffRows();
	const byPhase = (items, weight) => {
		const out = {};
		for (const item of items) {
			const phase = _phaseAt(transitions, item.t);
			out[phase] = (out[phase] ?? 0) + weight(item);
		}
		return out;
	};
	const record = {
		label,
		sid: sid.slice(0, 12),
		terminal,
		load_error: loadError,
		mix_ready_ms: loadResolvedAt === null ? null : Math.round(loadResolvedAt - t0),
		ready_ms: readyAt === null ? null : Math.round(readyAt - t0),
		phases: transitions.map(([phase, at]) => [phase, Math.round(at - t0)]),
		stages: stageRow?.stages ?? null,
		stage_labels: stageRow?.labels ?? null,
		pressure: loadRow?.labels
			? { load_avg_1m: loadRow.labels.load_avg_1m, swap_used_mb: loadRow.labels.swap_used_mb, kernel_level: loadRow.labels.kernel_level }
			: null,
		playing_decks: playing,
		playing_advanced_ms: Object.fromEntries(playing.map((d) => [d, Math.round(m.deckAudioClockPositionMs(d) - playingPosBefore[d])])),
		wall_ms: Math.round(tEnd - t0),
		// A deck that reached the end of its track still reads `playing` but
		// renders nothing: a run beside it is not a run beside a playing deck.
		other_decks_kept_playing: playing.length === 0
			? null
			: playing.every((d) => m.deckAudioClockPositionMs(d) - playingPosBefore[d] >= 0.9 * (tEnd - t0)),
		// A hidden tab has its timers throttled, which stretches the app's own
		// holds and this scorer's polling: a run is only comparable when visible.
		visibility: [visibilityAtStart, document.visibilityState],
		poll_period_ms: polls === 0 ? null : +((tEnd - t0) / polls).toFixed(1),
		underrun_events: statsBefore === null || statsAfter === null ? null : statsAfter.events - statsBefore.events,
		underrun_ms: statsBefore === null || statsAfter === null ? null : +(statsAfter.ms - statsBefore.ms).toFixed(2),
		underruns_by_phase: byPhase(underrunTimeline, (u) => u.events),
		sentinel_reports: probeReports.length,
		sentinel_threshold_ms: probe.thresholdMs,
		sentinel_xruns: sentinelEvents.reduce((sum, r) => sum + r.xruns, 0),
		sentinel_worst_gap_ms: sentinelEvents.reduce((max, r) => Math.max(max, r.worst), 0),
		sentinel_by_phase: byPhase(sentinelEvents, (r) => r.xruns),
		app_xruns: appXrunsAfter.xruns - appXrunsBefore.xruns,
		render_load: _renderLoad(loadProbe, loadFrom, t0),
		longtask_instrument: longTaskState.status,
		longtask_control_seen_ms: longTaskState.control_seen_ms,
		longtask_count: inRunOrNull === null ? null : inRun.length,
		longtask_total_ms: inRunOrNull === null ? null : Math.round(inRun.reduce((sum, task) => sum + task.dur, 0)),
		longtask_max_ms: inRunOrNull === null ? null : Math.round(inRun.reduce((max, task) => Math.max(max, task.dur), 0)),
		longtask_ms_by_phase: inRunOrNull === null ? null : byPhase(inRun, (task) => Math.round(task.dur)),
		play_when_mix_ready: playWhenMixReady,
		play_error: playError,
		load_deck_playing_at_end: m.deckStates[loadDeck].playing,
		// Audio clock advance of the load deck since play, against wall time: a
		// handoff that stopped or jumped the deck shows as a mismatch.
		load_deck_advanced_ms: playAt === null ? null : Math.round(m.deckAudioClockPositionMs(loadDeck) - posAtPlay),
		load_deck_wall_since_play_ms: playAt === null ? null : Math.round(tEnd - playAt),
		// Steps of the load deck's TRANSPORT clock that disagree with wall time
		// by more than 40 ms, with when they happened. A beat-sync join at play
		// start is one; one at `handoff_at_ms` would be the handoff moving the
		// deck. This is the transport model, NOT the audio: a click or a short
		// gap in the sound at the switch is not visible here.
		load_deck_clock_steps: playAt === null ? null : _clockSteps(positionSamples, t0),
		handoff_at_ms: readyAt === null ? null : Math.round(readyAt - t0),
		// The engine logs one 'stem-live-handoff' row when stems take over a
		// playing deck without stopping it. Counted before and after the run.
		live_handoffs: handoffRowsAfter.length - handoffRowsBefore.length,
		live_handoff_message: handoffRowsAfter.length > handoffRowsBefore.length
			? handoffRowsAfter[handoffRowsAfter.length - 1].message ?? null
			: null,
		heap_base_mb: heapBase === null ? null : Math.round(heapBase),
		heap_peak_mb: heapPeak === null ? null : Math.round(heapPeak),
		pcm_mb: Math.round(m.deckPcmEstimatedBytes() / 1048576)
	};
	(window.__stemLoadRuns ??= []).push(record);
	return record;
}

/**
 * The NEGATIVE CONTROL: the same dropout counters over a window in which no
 * load happens. A dropout count during a load means nothing until the rate
 * with no load is known, on the same machine, in the same minute.
 */
export async function runIdle({ label, durationMs = 20000 }) {
	const m = await _engine();
	const ctx = _context(m);
	const probe = await _armProbe(ctx);
	const loadProbe = await _armLoadProbe(ctx);
	const loadFrom = loadProbe.windows.length;
	const playing = Object.keys(m.deckStates).map(Number).filter((deck) => m.deckStates[deck].playing);
	const appBefore = await window.__mdtFlushXruns();
	const statsBefore = _playbackStats(ctx);
	const probeFrom = probe.reports.length;
	const t0 = performance.now();
	await _sleep(durationMs);
	const appAfter = await window.__mdtFlushXruns();
	const statsAfter = _playbackStats(ctx);
	const events = probe.reports.slice(probeFrom).filter((r) => r.t >= t0 && r.xruns > 0);
	const record = {
		label,
		idle: true,
		duration_ms: Math.round(performance.now() - t0),
		playing_decks: playing,
		visibility: document.visibilityState,
		underrun_events: statsBefore === null || statsAfter === null ? null : statsAfter.events - statsBefore.events,
		sentinel_xruns: events.reduce((sum, r) => sum + r.xruns, 0),
		sentinel_worst_gap_ms: events.reduce((max, r) => Math.max(max, r.worst), 0),
		sentinel_threshold_ms: probe.thresholdMs,
		app_xruns: appAfter.xruns - appBefore.xruns,
		render_load: _renderLoad(loadProbe, loadFrom, t0),
		decks: Object.fromEntries(Object.keys(m.deckStates).map((deck) => [deck,
			m.deckStates[deck].stable_id === null ? 'empty' : `${m.deckStates[deck].playing ? 'playing' : 'stopped'}/${m.deckStates[deck].stems.status}`]))
	};
	(window.__stemIdleRuns ??= []).push(record);
	return record;
}

/** Runs each sid once on `loadDeck`, unloading between runs. */
export async function runCondition({ label, loadDeck, sids, restMs = 4000, idleMs = 8000, playWhenMixReady = false }) {
	const m = await _engine();
	const out = [];
	window.__stemLoadProgress = { label, done: 0, of: sids.length, finished: false };
	for (const sid of sids) {
		if (m.deckStates[loadDeck].stable_id !== null) {
			await m.engine.unload(loadDeck);
			await _sleep(restMs);
		}
		// The background window for THIS run, in the same minute and deck state.
		if (idleMs > 0) await runIdle({ label, durationMs: idleMs });
		out.push(await runStemLoad({ loadDeck, sid, label, playWhenMixReady }));
		window.__stemLoadProgress.done += 1;
	}
	await m.engine.unload(loadDeck);
	await _sleep(restMs);
	window.__stemLoadProgress.finished = true;
	return out;
}

function _median(values) {
	const sorted = values.filter((v) => typeof v === 'number').sort((a, b) => a - b);
	if (sorted.length === 0) return null;
	const mid = Math.floor(sorted.length / 2);
	return sorted.length % 2 === 1 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

function _worst(values) {
	const numeric = values.filter((v) => typeof v === 'number');
	return numeric.length === 0 ? null : Math.max(...numeric);
}

/** P(X >= k) for X ~ Poisson(lambda). */
export function poissonUpperTail(k, lambda) {
	if (!(lambda >= 0) || !Number.isInteger(k) || k < 0) throw new RangeError('poissonUpperTail: bad input');
	if (k === 0) return 1;
	let term = Math.exp(-lambda);
	let below = term;
	for (let i = 1; i < k; i += 1) {
		term *= lambda / i;
		below += term;
	}
	return Math.max(0, 1 - below);
}

/** The rate above which `count` or fewer events in `seconds` has probability
 * 0.05: the highest background rate the idle windows cannot rule out. With no
 * idle event this is the rule of three (3 / seconds). */
export function poissonRateUpper95(count, seconds) {
	if (!(seconds > 0) || !Number.isInteger(count) || count < 0) throw new RangeError('poissonRateUpper95: bad input');
	let low = 0;
	let high = count + 10 * Math.sqrt(count + 1) + 10;
	for (let i = 0; i < 80; i += 1) {
		const mid = (low + high) / 2;
		// P(X <= count | mid) = 1 - P(X >= count + 1 | mid)
		if (1 - poissonUpperTail(count + 1, mid) > 0.05) low = mid;
		else high = mid;
	}
	return high / seconds;
}

/**
 * Late callbacks during loads against what the idle windows of the same
 * condition predict. `p_at_least_observed` assumes independent arrivals;
 * late callbacks on a busy host arrive in clusters, so the true tail is
 * heavier and a small p here is weaker evidence than it looks. With no idle
 * window the result is null: an unmeasured background is not a zero.
 */
export function aboveBackground(runs, idles) {
	const idleSeconds = idles.reduce((sum, idle) => sum + idle.duration_ms, 0) / 1000;
	if (idleSeconds <= 0) return null;
	const idleLate = idles.reduce((sum, idle) => sum + idle.sentinel_xruns, 0);
	const rate = idleLate / idleSeconds;
	const loadSeconds = runs.reduce((sum, run) => sum + run.wall_ms, 0) / 1000;
	const observed = runs.reduce((sum, run) => sum + run.sentinel_xruns, 0);
	const expected = rate * loadSeconds;
	return {
		idle_windows: idles.length,
		idle_seconds: +idleSeconds.toFixed(1),
		idle_late: idleLate,
		background_late_per_s: +rate.toFixed(3),
		load_seconds: +loadSeconds.toFixed(1),
		late_observed: observed,
		late_expected_from_background: +expected.toFixed(2),
		late_above_background: +(observed - expected).toFixed(2),
		p_at_least_observed: +poissonUpperTail(observed, expected).toFixed(3),
		// The same question against the HIGHEST background the idle windows
		// allow. Short idle windows with no event prove little: this is the
		// figure to read before calling a small excess real.
		background_late_per_s_upper95: +poissonRateUpper95(idleLate, idleSeconds).toFixed(3),
		p_at_least_observed_at_upper95: +poissonUpperTail(observed, poissonRateUpper95(idleLate, idleSeconds) * loadSeconds).toFixed(3),
		idle_worst_gap_ms: _worst(idles.map((idle) => idle.sentinel_worst_gap_ms)),
		load_worst_gap_ms: _worst(runs.map((run) => run.sentinel_worst_gap_ms))
	};
}

/** Median and worst per metric, grouped by condition label. */
export function summarize(runs, idles = window.__stemIdleRuns ?? []) {
	const labels = [...new Set(runs.map((run) => run.label))];
	const metrics = [
		'ready_ms', 'underrun_events', 'underrun_ms', 'sentinel_xruns', 'sentinel_worst_gap_ms',
		'app_xruns', 'longtask_count', 'longtask_total_ms', 'longtask_max_ms', 'heap_peak_mb'
	];
	const stages = ['probeStem', 'fetchStems', 'decodeStems', 'stemProcessorCreate', 'landStems'];
	return labels.map((label) => {
		const group = runs.filter((run) => run.label === label);
		const row = {
			label,
			runs: group.length,
			not_ready: group.filter((run) => run.terminal !== 'ready').length,
			// Runs whose "playing" deck had stopped rendering: not evidence for a
			// with-playback condition. Computed from the fields every round kept.
			other_deck_stalled: group.filter((run) =>
				run.playing_decks.some((d) => run.playing_advanced_ms[d] < 0.9 * run.wall_ms)).length
		};
		for (const metric of metrics) {
			const values = group.map((run) => run[metric]);
			// A sum over a run that could not be measured is not a smaller sum.
			const measured = values.every((v) => typeof v === 'number');
			row[metric] = { median: _median(values), worst: _worst(values), sum: measured ? values.reduce((s, v) => s + v, 0) : null };
		}
		for (const stage of stages) {
			const values = group.map((run) => run.stages?.[stage]);
			row[`stage_${stage}`] = { median: _median(values), worst: _worst(values) };
		}
		const waits = group.map((run) => Number(run.stage_labels?.decode_wait_ms));
		row.decode_wait_ms = { median: _median(waits), worst: _worst(waits) };
		row.background = aboveBackground(group, idles.filter((idle) => idle.label === label));
		row.longtask_instrument = [...new Set(group.map((run) => run.longtask_instrument))];
		row.heap_growth_mb = {
			median: _median(group.map((run) => run.heap_peak_mb - run.heap_base_mb)),
			worst: _worst(group.map((run) => run.heap_peak_mb - run.heap_base_mb))
		};
		return row;
	});
}

// ------------------------------------------------------- attribution bench

function _segment(probe, loadProbe, name, t0, t1) {
	const reports = probe.reports.filter((r) => r.t >= t0 && r.t <= t1 + SENTINEL_REPORT_MS);
	const windows = loadProbe.windows.filter((w) => w.t >= t0 && w.t <= t1 + SENTINEL_REPORT_MS);
	return {
		name,
		ms: Math.round(t1 - t0),
		late: reports.reduce((sum, r) => sum + r.xruns, 0),
		worst_gap_ms: windows.reduce((max, w) => Math.max(max, w.max), 0)
	};
}

/**
 * Which part of a stem load makes render callbacks late, isolated from the
 * engine: the same encoded bundle is decoded 4-wide and 1-wide, and the stem
 * processors are built and thrown away, with an idle window between each so
 * the machine's own background rate is measured in the same minute. Nothing
 * lands on a deck. Interleaved on purpose: the background rate on a busy
 * machine moves faster than a condition takes to run.
 */
export async function benchLoadShapes({ sid, iterations = 6, idleMs = 3000 }) {
	const m = await _engine();
	if (!m.isMasterMuted()) throw new Error('scorer: master must be muted');
	const ctx = _context(m);
	const probe = await _armProbe(ctx);
	const loadProbe = await _armLoadProbe(ctx);
	const graph = await _pageModule('/src/lib/rb/stem-graph.ts');
	const parts = ['vocals', 'drums', 'bass', 'other'];
	const encoded = await Promise.all(parts.map(async (part) => {
		const response = await fetch(`/api/v1/tracks/${sid}/stems/${part}`);
		if (!response.ok) throw new Error(`bench: stem ${part} fetch failed (${response.status})`);
		return response.arrayBuffer();
	}));
	const segments = [];
	const timed = async (name, run) => {
		const t0 = performance.now();
		const value = await run();
		segments.push(_segment(probe, loadProbe, name, t0, performance.now()));
		return value;
	};
	const build = async (name, buffers) => {
		const created = await timed(name, () => graph.AlignedStemDeckProcessor.create(
			ctx, Object.fromEntries(parts.map((part, i) => [part, buffers[i]])),
			{ onProcessorError: () => {} }
		));
		await _sleep(300);
		await timed('dispose', () => created.processor.dispose());
	};
	window.__stemBench = { done: 0, of: iterations, finished: false, segments };
	for (let i = 0; i < iterations; i += 1) {
		await timed('idle', () => _sleep(idleMs));
		const wide = await timed('decode-4-wide', () =>
			Promise.all(encoded.map((bytes) => ctx.decodeAudioData(bytes.slice(0)))));
		await timed('idle', () => _sleep(idleMs));
		await build('create-processors', wide);
		await timed('idle', () => _sleep(idleMs));
		const narrow = [];
		await timed('decode-1-wide', async () => {
			for (const bytes of encoded) narrow.push(await ctx.decodeAudioData(bytes.slice(0)));
		});
		narrow.length = 0;
		await timed('idle', () => _sleep(idleMs));
		await timed('decode-2-wide', async () => {
			for (let at = 0; at < encoded.length; at += 2) {
				await Promise.all(encoded.slice(at, at + 2).map((bytes) => ctx.decodeAudioData(bytes.slice(0))));
			}
		});
		window.__stemBench.done += 1;
	}
	window.__stemBench.finished = true;
	return summarizeSegments(segments);
}

/** Per segment kind: total time, late callbacks, late per second, worst gap. */
export function summarizeSegments(segments) {
	const names = [...new Set(segments.map((segment) => segment.name))];
	return names.map((name) => {
		const group = segments.filter((segment) => segment.name === name);
		const ms = group.reduce((sum, segment) => sum + segment.ms, 0);
		const late = group.reduce((sum, segment) => sum + segment.late, 0);
		return {
			name,
			n: group.length,
			median_ms: _median(group.map((segment) => segment.ms)),
			late,
			late_per_s: +(late / (ms / 1000)).toFixed(2),
			segments_with_late: group.filter((segment) => segment.late > 0).length,
			worst_gap_ms: _worst(group.map((segment) => segment.worst_gap_ms))
		};
	});
}

// ------------------------------------------------ processor creation bench

const STRETCH_SOURCE_URL = '/node_modules/signalsmith-stretch/SignalsmithStretch.mjs';
const STRETCH_KEY_LITERAL = '})(SignalsmithStretch, "signalsmith-stretch");';
const STRETCH_INSTANTIATE =
	'function instantiateArrayBuffer(binaryFile,imports,receiver){return getBinaryPromise(binaryFile).then(binary=>WebAssembly.instantiate(binary,imports)).then(receiver,';
const STRETCH_CONSTRUCT = 'Module().then(wasmModule => {';

/**
 * Source transforms of the SHIPPED stretch module, one per candidate. Each is
 * registered under its own processor name so all of them coexist in one
 * AudioContext and can be interleaved. A transform whose anchor is missing
 * throws: a variant that silently equals the baseline would measure nothing.
 *   as-shipped      whatever the installed (patched) module does today
 *   per-processor   upstream behavior: every processor decodes the embedded
 *                   base64 and compiles the module on the audio thread
 *   compile-once    the worklet scope decodes and compiles once, later
 *                   processors only instantiate
 *   main-compiled   the main thread compiles; the WebAssembly.Module reaches
 *                   the worklet by structured clone in processorOptions
 */
const STRETCH_VARIANTS = {
	'as-shipped': (source) => source,
	'compile-once': (source) => _swap(source, STRETCH_INSTANTIATE,
		'function instantiateArrayBuffer(binaryFile,imports,receiver){return (globalThis.__benchStretchModule??=getBinaryPromise(binaryFile).then(binary=>WebAssembly.compile(binary))).then(compiled=>WebAssembly.instantiate(compiled,imports)).then(instance=>({instance})).then(receiver,'),
	'main-compiled': (source) => _swap(
		_swap(source, STRETCH_CONSTRUCT, 'Module({benchCompiled: options.processorOptions.compiled}).then(wasmModule => {'),
		STRETCH_INSTANTIATE,
		'function instantiateArrayBuffer(binaryFile,imports,receiver){return WebAssembly.instantiate(Module.benchCompiled,imports).then(instance=>({instance})).then(receiver,')
};

function _swap(source, anchor, replacement) {
	const parts = source.split(anchor);
	if (parts.length !== 2) throw new Error(`bench: anchor found ${parts.length - 1} times, wanted 1: ${anchor.slice(0, 60)}`);
	return parts.join(replacement);
}

/** The adapter's own disposal order. Without the port close the processor
 * (and its WASM memory) stays reachable, and about 115 leaked instances later
 * the worklet cannot allocate another one. */
async function _disposeStretchNode(node) {
	node.disconnect();
	await node.dispose();
	node.port.onmessage = null;
	node.port.close();
}

async function _stretchVariant(ctx, name, source) {
	const transform = STRETCH_VARIANTS[name];
	if (transform === undefined) throw new Error(`bench: unknown variant ${name}`);
	const key = `bench-stretch-${name}`;
	const code = _swap(transform(source), STRETCH_KEY_LITERAL, `})(SignalsmithStretch, "${key}");`);
	const url = URL.createObjectURL(new Blob([code], { type: 'text/javascript' }));
	const factory = (await _nativeImport(url)).default;
	factory.moduleUrl = url;
	let compiled = null;
	if (name === 'main-compiled') {
		const base64 = /data:application\/octet-stream;base64,([A-Za-z0-9+/=]+)"/.exec(source);
		if (base64 === null) throw new Error('bench: embedded wasm not found');
		compiled = await WebAssembly.compile(Uint8Array.from(atob(base64[1]), (c) => c.charCodeAt(0)));
	}
	const options = () => ({
		numberOfInputs: 1,
		numberOfOutputs: 1,
		outputChannelCount: [2],
		...(compiled === null ? {} : { processorOptions: { compiled } })
	});
	// The first node pays addModule (and, for compile-once, the one compile):
	// that is the app's first deck, not the per-load cost under test.
	const warm = await factory(ctx, options());
	await _disposeStretchNode(warm);
	return { name, create: () => factory(ctx, options()) };
}

/**
 * Late render callbacks caused by creating the four stretch processors of one
 * stem bundle, per candidate, with a deck playing. Candidates are interleaved
 * and their order rotates each iteration, with an idle window after every
 * bundle, because the host's background rate moves faster than a candidate
 * takes to run. Nothing lands on a deck and no audio is loaded: this isolates
 * processor construction from the PCM transfer that follows it.
 *
 * `staggerMs` waits between the four creates of a bundle (0 = back to back).
 */
export async function benchProcessorCreation({
	variants = ['as-shipped', 'compile-once', 'main-compiled'],
	iterations = 30,
	idleMs = 1500,
	staggerMs = [0],
	perBundle = 4,
	sid = null,
	pcmCells = ['pcm-copy-only', 'load:copy', 'load:transfer', 'product-create']
}) {
	const m = await _engine();
	if (!m.isMasterMuted()) throw new Error('scorer: master must be muted');
	const ctx = _context(m);
	const probe = await _armProbe(ctx);
	const loadProbe = await _armLoadProbe(ctx);
	const response = await fetch(STRETCH_SOURCE_URL);
	if (!response.ok) throw new Error(`bench: stretch source fetch failed (${response.status})`);
	const source = await response.text();
	const built = [];
	for (const name of variants) built.push(await _stretchVariant(ctx, name, source));
	const parts = ['vocals', 'drums', 'bass', 'other'];
	const graph = await _pageModule('/src/lib/rb/stem-graph.ts');
	const adapter = await _pageModule('/src/lib/rb/stretch-adapter.ts');
	let encoded = null;
	if (sid !== null) {
		encoded = await Promise.all(parts.map(async (part) => {
			const stem = await fetch(`/api/v1/tracks/${sid}/stems/${part}`);
			if (!stem.ok) throw new Error(`bench: stem ${part} fetch failed (${stem.status})`);
			return stem.arrayBuffer();
		}));
	}
	const cells = built.flatMap((variant) => staggerMs.map((gap) => ({ variant, gap })));
	const segments = [];
	window.__stemBench = { done: 0, of: iterations, finished: false, segments, error: null };
	try {
		for (let i = 0; i < iterations; i += 1) {
			for (let j = 0; j < cells.length; j += 1) {
				const { variant, gap } = cells[(i + j) % cells.length];
				const idleStart = performance.now();
				await _sleep(idleMs);
				segments.push(_segment(probe, loadProbe, 'idle', idleStart, performance.now()));
				const nodes = [];
				const t0 = performance.now();
				for (let k = 0; k < perBundle; k += 1) {
					if (k > 0 && gap > 0) await _sleep(gap);
					nodes.push(await variant.create());
				}
				// Construction work can outlive the ready handshake by a quantum.
				await _sleep(60);
				const segment = _segment(probe, loadProbe, `${variant.name}${gap > 0 ? `+stagger${gap}` : ''}`, t0, performance.now());
				segment.sleep_ms = 60 + gap * (perBundle - 1);
				segments.push(segment);
				for (const node of nodes) await _disposeStretchNode(node);
			}
			if (sid !== null) {
				// The PCM handoff, isolated and then in the product path. Every
				// cell decodes its own bundle (a moved bundle cannot be reused)
				// one part at a time, then idles, so the decode is outside the
				// measured window.
				for (let j = 0; j < pcmCells.length; j += 1) {
					const cell = pcmCells[(i + j) % pcmCells.length];
					const decoded = [];
					for (const bytes of encoded) decoded.push(await ctx.decodeAudioData(bytes.slice(0)));
					let t0 = performance.now();
					await _sleep(idleMs);
					segments.push(_segment(probe, loadProbe, 'idle', t0, performance.now()));
					t0 = performance.now();
					let dispose = async () => {};
					let note = null;
					if (cell === 'pcm-copy-only') {
						let copies = decoded.flatMap((buffer) => Array.from({ length: buffer.numberOfChannels }, (_unused, channel) => {
							const samples = new Float32Array(buffer.length);
							buffer.copyFromChannel(samples, channel);
							return samples;
						}));
						note = copies.length;
						copies = null;
					} else if (cell === 'product-create') {
						const created = await graph.AlignedStemDeckProcessor.create(
							ctx, Object.fromEntries(parts.map((part, at) => [part, decoded[at]])),
							{ onProcessorError: () => {} }
						);
						note = created.pcmHandoff;
						dispose = () => created.processor.dispose();
					} else {
						// The product's per-part sequence with only the handoff varied.
						const requested = cell === 'load:copy' ? 'copy' : 'transfer';
						const processors = [];
						const happened = [];
						for (const buffer of decoded) {
							const processor = await adapter.StretchDeckProcessor.create(ctx, { onProcessorError: () => {} });
							happened.push(await processor.load(buffer, requested));
							const gain = ctx.createGain();
							processor.connect(gain);
							processors.push(processor);
						}
						note = [...new Set(happened)].join('+');
						dispose = async () => { for (const processor of processors) await processor.dispose(); };
					}
					await _sleep(60);
					const segment = _segment(probe, loadProbe, cell, t0, performance.now());
					segment.sleep_ms = 60;
					segment.note = note;
					segments.push(segment);
					await dispose();
				}
			}
			window.__stemBench.done += 1;
		}
	} catch (error) {
		window.__stemBench.error = String(error);
		throw error;
	} finally {
		window.__stemBench.finished = true;
	}
	return summarizeSegments(segments);
}
