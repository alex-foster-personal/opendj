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

let _ticks = 0;
let _windowStart = 0;
let _hz = $state<number | null>(null);
let _level = $state<AudioHealthLevel>('idle');
let _samplerId: ReturnType<typeof setInterval> | null = null;

function _ensureSampler(): void {
	if (_samplerId !== null || typeof window === 'undefined') return;
	_windowStart = performance.now();
	_samplerId = setInterval(() => {
		const now = performance.now();
		const elapsed = now - _windowStart;
		if (elapsed <= 0) return;
		const hz = (_ticks * 1000) / elapsed;
		_ticks = 0;
		_windowStart = now;
		if (hz < 1) {
			_hz = null;
			_level = 'idle';
			// Idle exit: a whole window with no engine ticks means nothing is
			// audible. noteAudioPresentationTick re-arms on the next publish,
			// so the readout stays live without polling through idle.
			_stopSampler();
			return;
		}
		const rounded = Math.round(hz);
		_hz = rounded;
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

/** Hover copy for the TopBar Hz readout. */
export function audioHealthHover(): string {
	return (
		'Audio presentation publish rate (Hz) while a deck is audible - ' +
		'like game FPS for the output clock. Orange <45, red <30. ' +
		'Idle when nothing is playing.'
	);
}
