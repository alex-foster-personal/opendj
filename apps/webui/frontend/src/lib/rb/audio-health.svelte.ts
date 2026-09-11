/**
 * Live audio presentation health (performance feature).
 *
 * Latency / reliability role: this is the audio analogue of game FPS -
 * how often the engine successfully publishes output-timestamp-backed
 * transport while a deck is audible. Drops under load (GC, main-thread
 * stalls, audio callback pressure) go orange/red in the TopBar so we can
 * catch RAM-induced skipping before a demo.
 *
 * Wired from `audio-engine._tick` via {@link noteAudioPresentationTick}.
 * Does not alter scheduling, decode, or prefetch - readout only.
 *
 * Thresholds (while any deck audible/pending):
 * - healthy: >= 45 Hz
 * - warn (orange): 30..44 Hz
 * - crit (red): < 30 Hz
 */
export type AudioHealthLevel = 'idle' | 'ok' | 'warn' | 'crit';

const WARN_HZ = 45;
const CRIT_HZ = 30;
const SAMPLE_MS = 500;

/** Locked aliases for tests and the PerfMeters monitor (issue #1984). */
export const HZ_METER_OK_HZ = WARN_HZ;
export const HZ_METER_CRIT_HZ = CRIT_HZ;
export const HZ_METER_SAMPLE_MS = SAMPLE_MS;
export const HZ_METER_MAX_ABS_ERROR_HZ = 2;

let _ticks = 0;
let _windowStart = 0;
let _hz = $state<number | null>(null);
let _level = $state<AudioHealthLevel>('idle');
let _tickHzRaw = $state<number | null>(null);
let _qualityOk = $state(true);
let _samplerId: ReturnType<typeof setInterval> | null = null;

export function presentationTickHz(ticks: number, elapsedMs: number): number {
	if (elapsedMs <= 0) return 0;
	return (ticks * 1000) / elapsedMs;
}

export function hzMeterAbsError(meterHz: number, tickHz: number): number {
	return Math.abs(meterHz - tickHz);
}

export function hzMeterQualityOk(meterHz: number, tickHz: number): boolean {
	return hzMeterAbsError(meterHz, tickHz) <= HZ_METER_MAX_ABS_ERROR_HZ;
}

/** Waveform paint cadence, not audio or device health. A stutter is a gap over
 * two 60 Hz frames (34ms) during a real visible waveform draw. The latest
 * 2s draw window is intentionally unsmoothed so a single dropped frame stays
 * inspectable through UI and public IPC. */
const WAVEFORM_STUTTER_WINDOW_MS = 2000;
const WAVEFORM_STUTTER_GAP_MS = 34;
interface WavePaintCadence { previousMs: number; frames: number; stutters: number; worstGapMs: number; }
const _waveRows = new Map<number, WavePaintCadence>();
let _waveWindowStartMs: number | null = null;
let _waveSnapshot = $state({ active: false, frames: 0, stutters: 0, worst_gap_ms: 0, elapsed_ms: 0, window_ms: WAVEFORM_STUTTER_WINDOW_MS, threshold_ms: WAVEFORM_STUTTER_GAP_MS });

function _publishWaveformWindow(elapsedMs = 0): void {
	const rows = [..._waveRows.values()];
	_waveSnapshot = {
		active: rows.length > 0,
		frames: rows.reduce((total, row) => total + row.frames, 0),
		stutters: rows.reduce((total, row) => total + row.stutters, 0),
		worst_gap_ms: Math.round(rows.reduce((worst, row) => Math.max(worst, row.worstGapMs), 0)),
		elapsed_ms: Math.round(elapsedMs),
		window_ms: WAVEFORM_STUTTER_WINDOW_MS,
		threshold_ms: WAVEFORM_STUTTER_GAP_MS
	};
}

/** Called from the actual WaveRow rAF callback with its browser timestamp. */
export function noteWaveformPaintFrame(deck: number, nowMs: number): void {
	if (!Number.isFinite(nowMs)) throw new TypeError(`waveform paint timestamp must be finite, got ${nowMs}`);
	let row = _waveRows.get(deck);
	if (row === undefined) {
		row = { previousMs: nowMs, frames: 1, stutters: 0, worstGapMs: 0 };
		_waveRows.set(deck, row);
		if (_waveWindowStartMs === null) _waveWindowStartMs = nowMs;
		_publishWaveformWindow(nowMs - _waveWindowStartMs);
		return;
	}
	const gapMs = nowMs - row.previousMs;
	row.previousMs = nowMs;
	row.frames += 1;
	row.worstGapMs = Math.max(row.worstGapMs, gapMs);
	if (gapMs > WAVEFORM_STUTTER_GAP_MS) row.stutters += 1;
	const elapsedMs = nowMs - (_waveWindowStartMs ?? nowMs);
	if (elapsedMs < WAVEFORM_STUTTER_WINDOW_MS) return;
	_publishWaveformWindow(elapsedMs);
	_waveWindowStartMs = nowMs;
	for (const activeRow of _waveRows.values()) {
		activeRow.frames = 0;
		activeRow.stutters = 0;
		activeRow.worstGapMs = 0;
	}
}

/** Stop/hidden/unmounted rows are unavailable, never synthetic stutters. */
export function resetWaveformPaintCadence(deck: number): void {
	_waveRows.delete(deck);
	if (_waveRows.size === 0) _waveWindowStartMs = null;
	_publishWaveformWindow();
}

export function waveformStutterSnapshot(): Readonly<typeof _waveSnapshot> {
	return _waveSnapshot;
}

export function waveformStutterHover(): string {
	const metric = _waveSnapshot;
	return metric.active
		? `Waveform paint stutters: ${metric.stutters} frame gap(s) over ${metric.threshold_ms}ms in the measured ${metric.elapsed_ms}ms window (${metric.frames} draws, worst gap ${metric.worst_gap_ms}ms). This measures visual canvas cadence, not audio glitches.`
		: 'Waveform paint stutter KPI is inactive until a visible waveform draws.';
}

function _ensureSampler(): void {
	if (_samplerId !== null || typeof window === 'undefined') return;
	_windowStart = performance.now();
	_samplerId = setInterval(() => {
		const now = performance.now();
		const elapsed = now - _windowStart;
		if (elapsed <= 0) return;
		const tickHz = presentationTickHz(_ticks, elapsed);
		_ticks = 0;
		_windowStart = now;
		if (tickHz < 1) {
			_hz = null;
			_tickHzRaw = null;
			_qualityOk = true;
			_level = 'idle';
			// Idle exit: a whole window with no engine ticks means nothing is
			// audible. noteAudioPresentationTick re-arms on the next publish,
			// so the readout stays live without polling through idle.
			_stopSampler();
			return;
		}
		const rounded = Math.round(tickHz);
		_hz = rounded;
		_tickHzRaw = tickHz;
		_qualityOk = hzMeterQualityOk(rounded, tickHz);
		if (rounded < CRIT_HZ) _level = 'crit';
		else if (rounded < WARN_HZ) _level = 'warn';
		else _level = 'ok';
	}, SAMPLE_MS);
}

function _stopSampler(): void {
	if (_samplerId === null) return;
	clearInterval(_samplerId);
	_samplerId = null;
}

/** Called once per engine presentation publish while transport is live. */
export function noteAudioPresentationTick(): void {
	_ensureSampler();
	_ticks += 1;
}

export function audioHealthHz(): number | null {
	return _hz;
}

export function audioHealthLevel(): AudioHealthLevel {
	return _level;
}

export function audioHealthQuality(): {
	ok: boolean;
	meterHz: number | null;
	tickHz: number | null;
	absError: number | null;
} {
	if (_hz === null || _tickHzRaw === null) {
		return { ok: true, meterHz: _hz, tickHz: _tickHzRaw, absError: null };
	}
	return {
		ok: _qualityOk,
		meterHz: _hz,
		tickHz: _tickHzRaw,
		absError: hzMeterAbsError(_hz, _tickHzRaw)
	};
}

/** Hover copy for the TopBar Hz readout. */
export function audioHealthHover(): string {
	return (
		'Audio presentation publish rate (Hz) while a deck is audible - ' +
		'like game FPS for the output clock. Orange <45, red <30. ' +
		'Idle when nothing is playing.'
	);
}
