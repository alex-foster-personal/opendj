/**
 * Janky engagement "vibe" meter: mouse movement tops up a linear charge that
 * decays when idle; display is an S-curve of that charge. Sensitivity + decay
 * come from central BE config (GET /api/v1/settings). Samples land in
 * localStorage so we can later correlate high-vibe stretches with what was
 * on the decks.
 *
 * Not part of the performance command path - chrome-only signal for now.
 */

import { getSettings } from '$lib/api';

const STORAGE_KEY = 'mdt.rb.vibe-history.v1';
const HISTORY_CAP = 400;
const SAMPLE_MS = 2000;
/** Baseline px of travel for a full linear charge at sensitivity=1. */
const BASE_FILL_PX = 1000;
const MIN_MOVE_PX = 0.5;
/** Logistic steepness for display S-curve (normalized to 0..1). */
const S_CURVE_K = 10;
const VOTE_DELTA = 0.18;
/** Rainbow auto crawl (cycles / sec) - the "20% either way" baseline drift. */
const RAINBOW_AUTO = 0.2;
/** Max rainbow chase toward mouse (cycles / sec) - caps frantic jumps. */
const RAINBOW_MAX_RATE = 0.85;

export type VibeSource = 'move' | 'up' | 'down';

export interface VibeSample {
	/** Epoch ms (wall clock). */
	t: number;
	/** Display level 0..1 (S-curved) at sample time. */
	level: number;
	/** Peak display since page load. */
	peak: number;
	/** Pointer travel (px) in the sample window; 0 for manual votes. */
	moved: number;
	/** How this sample was produced. */
	source: VibeSource;
}

interface VibeHistoryBlob {
	samples: VibeSample[];
}

export const vibeState = $state({
	/** Linear charge 0..1 (pre S-curve). */
	charge: 0,
	/** Display level 0..1 after S-curve. */
	display: 0,
	peak: 0,
	/** Lifetime px moved this page load (provenance / debug). */
	moved_total: 0,
	/** From BE; applied once settings load. */
	sensitivity: 0.07,
	decay_per_sec: 0.05,
	config_ready: false,
	/** Unbounded rainbow phase (seamless via continuous background-position). */
	rainbow_index: 0
});

let _lastX = Number.NaN;
let _lastY = Number.NaN;
/** Absolute pointer X in 0..1 of viewport width. */
let _mouseNorm = 0.5;
let _windowMoved = 0;
let _lastSampleAt = 0;
let _raf = 0;
let _lastTick = 0;
let _listening = false;

// ----------------------------------------------------------- _helpers

function _storage(): Storage | null {
	return typeof window === 'undefined' ? null : window.localStorage;
}

function _clamp01(v: number): number {
	return Math.min(1, Math.max(0, v));
}

/** Normalized logistic so 0 -> 0 and 1 -> 1 (slow start, fast mid, soft top). */
export function vibeSCurve(x: number, k = S_CURVE_K): number {
	const t = _clamp01(x);
	const s = (u: number) => 1 / (1 + Math.exp(-k * (u - 0.5)));
	return (s(t) - s(0)) / (s(1) - s(0));
}

function _setCharge(next: number): void {
	vibeState.charge = _clamp01(next);
	vibeState.display = vibeSCurve(vibeState.charge);
	if (vibeState.display > vibeState.peak) vibeState.peak = vibeState.display;
}

function _loadHistory(): VibeSample[] {
	const raw = _storage()?.getItem(STORAGE_KEY);
	if (raw === null || raw === undefined) return [];
	const parsed = JSON.parse(raw) as Partial<VibeHistoryBlob>;
	if (!Array.isArray(parsed.samples)) {
		throw new Error(
			`${STORAGE_KEY}: malformed vibe history - clear the localStorage key to recover`
		);
	}
	return parsed.samples;
}

function _persistSample(sample: VibeSample): void {
	const storage = _storage();
	if (storage === null) return;
	const samples = _loadHistory();
	samples.push(sample);
	while (samples.length > HISTORY_CAP) samples.shift();
	storage.setItem(STORAGE_KEY, JSON.stringify({ samples } satisfies VibeHistoryBlob));
}

function _maybeSample(now: number): void {
	if (_lastSampleAt === 0) {
		_lastSampleAt = now;
		return;
	}
	if (now - _lastSampleAt < SAMPLE_MS) return;
	const moved = _windowMoved;
	_windowMoved = 0;
	_lastSampleAt = now;
	// Skip flat idle stretches so history stays about moments that mattered.
	if (moved < 1 && vibeState.display < 0.02) return;
	_persistSample({
		t: Date.now(),
		level: vibeState.display,
		peak: vibeState.peak,
		moved,
		source: 'move'
	});
}

function _settingNumber(items: { key: string; value: unknown }[], key: string): number | null {
	const item = items.find((i) => i.key === key);
	if (item === undefined) return null;
	const n = typeof item.value === 'number' ? item.value : Number(item.value);
	if (!Number.isFinite(n)) {
		throw new Error(`vibe config: ${key} is not a finite number (${String(item.value)})`);
	}
	return n;
}

async function _fetchConfig(): Promise<void> {
	const settings = await getSettings();
	const items = settings.groups.flatMap((g) => g.items);
	const sensitivity = _settingNumber(items, 'vibe_sensitivity');
	const decay = _settingNumber(items, 'vibe_decay_per_sec');
	if (sensitivity === null || decay === null) {
		throw new Error(
			'vibe config: vibe_sensitivity / vibe_decay_per_sec missing from /api/v1/settings'
		);
	}
	vibeState.sensitivity = sensitivity;
	vibeState.decay_per_sec = decay;
	vibeState.config_ready = true;
}

/** Manual thumb nudge - instant provenance mark + charge bump/drop. */
export function voteVibe(dir: 'up' | 'down'): void {
	const delta = dir === 'up' ? VOTE_DELTA : -VOTE_DELTA;
	_setCharge(vibeState.charge + delta);
	_persistSample({
		t: Date.now(),
		level: vibeState.display,
		peak: vibeState.peak,
		moved: 0,
		source: dir
	});
}

function _onPointerMove(e: PointerEvent): void {
	const w = window.innerWidth;
	if (w > 0) _mouseNorm = _clamp01(e.clientX / w);
	if (!Number.isFinite(_lastX)) {
		_lastX = e.clientX;
		_lastY = e.clientY;
		return;
	}
	const dx = e.clientX - _lastX;
	const dy = e.clientY - _lastY;
	_lastX = e.clientX;
	_lastY = e.clientY;
	const dist = Math.hypot(dx, dy);
	if (dist < MIN_MOVE_PX) return;
	_windowMoved += dist;
	vibeState.moved_total += dist;
	const fillPx = BASE_FILL_PX / Math.max(0.01, vibeState.sensitivity);
	_setCharge(vibeState.charge + dist / fillPx);
}

function _tickRainbow(dt: number): void {
	// Baseline crawl (20%/s) keeps motion alive; mouse absolute sets phase.
	vibeState.rainbow_index += RAINBOW_AUTO * dt;
	let err = _mouseNorm - (((vibeState.rainbow_index % 1) + 1) % 1);
	if (err > 0.5) err -= 1;
	if (err < -0.5) err += 1;
	const maxStep = RAINBOW_MAX_RATE * dt;
	vibeState.rainbow_index += Math.max(-maxStep, Math.min(maxStep, err));
}

function _tick(now: number): void {
	if (_lastTick === 0) _lastTick = now;
	const dt = Math.min(0.1, (now - _lastTick) / 1000);
	_lastTick = now;
	if (dt > 0 && vibeState.charge > 0) {
		_setCharge(vibeState.charge - vibeState.decay_per_sec * dt);
	}
	if (dt > 0 && vibeState.display >= 0.9) _tickRainbow(dt);
	_maybeSample(now);
	_raf = requestAnimationFrame(_tick);
}

// -------------------------------------------------------- public API

/** Start global pointer + decay loop; pull BE sensitivity. Idempotent. */
export function startVibeMeter(): void {
	if (_listening || typeof window === 'undefined') return;
	_listening = true;
	_lastTick = 0;
	_lastSampleAt = performance.now();
	window.addEventListener('pointermove', _onPointerMove, { passive: true });
	_raf = requestAnimationFrame(_tick);
	void _fetchConfig();
}

/** Stop listeners (TopBar / performance unmount). */
export function stopVibeMeter(): void {
	if (!_listening || typeof window === 'undefined') return;
	_listening = false;
	window.removeEventListener('pointermove', _onPointerMove);
	if (_raf !== 0) cancelAnimationFrame(_raf);
	_raf = 0;
	_lastX = Number.NaN;
	_lastY = Number.NaN;
	_lastTick = 0;
}

/** Read persisted samples (fail-fast on malformed blob). */
export function getVibeHistory(): VibeSample[] {
	return _loadHistory();
}
