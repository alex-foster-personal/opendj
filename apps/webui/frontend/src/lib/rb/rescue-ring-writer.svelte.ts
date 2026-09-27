/**
 * Gig-only rescue ring writer: cadence + transport triggers (RESCUE-01).
 * Main thread, fire-and-forget POST - never blocks command dispatch.
 */

import { API_BASE } from '$lib/api';
import type { DeckId } from '$lib/rb/deck-id';
import {
	buildRescueSnapshot,
	serializeRescueSnapshot,
	type RescueSnapshotReason
} from '$lib/rb/rescue-snapshot';
import {
	dispatchPerformanceCommand,
	queryPerformanceState,
	type PerformanceCommand
} from '$lib/rb/performance-ipc.svelte';
import { getDeckState } from '$lib/rb/audio-engine.svelte';
import { uiPrefs } from '$lib/rb/prefs.svelte';

export const RESCUE_RING_PERIOD_MS = 2000;
export const RESCUE_TRANSPORT_DEBOUNCE_MS = 50;

const LOAD_COMMANDS = new Set<PerformanceCommand['type']>(['load', 'unload']);
const TRANSPORT_COMMANDS = new Set<PerformanceCommand['type']>([
	'load',
	'unload',
	'play',
	'seek',
	'beat_jump',
	'beat_loop',
	'loop',
	'crossfader',
	'master_volume',
	'tempo',
	'pitch_range',
	'master',
	'output_mode',
	'headphone_mix',
	'headphone_level',
	'channel_cue',
	// RESCUE-05: a sink picked while nothing plays must still reach the ring;
	// the periodic snapshot only runs while a deck is playing.
	'headphone_output_select',
	'headphone_master_select',
	'headphone_output_acquire'
]);

const DECK_IDS: DeckId[] = [1, 2, 3, 4];

function _sourcePathsFromEngine(): Record<DeckId, string | null> {
	return {
		1: getDeckState(1).source_path,
		2: getDeckState(2).source_path,
		3: getDeckState(3).source_path,
		4: getDeckState(4).source_path
	};
}

function _anyDeckPlaying(state = queryPerformanceState()): boolean {
	return DECK_IDS.some((deckId) => state.decks[deckId].playing);
}

/** Pure cadence gate for unit tests and the periodic interval. */
export function shouldPostPeriodicRescue(
	now: number,
	lastPeriodicPostAt: number,
	playing: boolean,
	posture: string
): boolean {
	if (posture !== 'gig') return false;
	if (!playing) return false;
	if (now - lastPeriodicPostAt < RESCUE_RING_PERIOD_MS) return false;
	return true;
}

function _postSnapshot(reason: RescueSnapshotReason, now: number): void {
	if (uiPrefs.app_posture !== 'gig') return;
	const snapshot = buildRescueSnapshot(
		queryPerformanceState(),
		reason,
		now,
		_sourcePathsFromEngine()
	);
	const serialized = serializeRescueSnapshot(snapshot);
	if (serialized === _lastSuccessfulSerialized) return;
	_inFlight = true;
	void fetch(`${API_BASE}/api/v1/performance/rescue-snapshots`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: serialized
	})
		.then((response) => {
			if (response.ok) {
				_lastSuccessfulSerialized = serialized;
				if (reason === 'periodic') _lastPeriodicPostAt = now;
			}
		})
		.catch(() => {})
		.finally(() => {
			_inFlight = false;
			if (_pendingTransportSnapshot) {
				_pendingTransportSnapshot = false;
				_postSnapshot(_pendingTransportReason, Date.now());
			}
		});
}

let _lastSuccessfulSerialized: string | null = null;
let _lastPeriodicPostAt = 0;
let _inFlight = false;
let _pendingTransportSnapshot = false;
let _transportDebounceTimer: ReturnType<typeof setTimeout> | null = null;
let _pendingTransportReason: RescueSnapshotReason = 'transport';
let _intervalId: ReturnType<typeof setInterval> | null = null;
let _hooksInstalled = false;

export function notifyRescueTransportEvent(command: PerformanceCommand): void {
	if (!_hooksInstalled || uiPrefs.app_posture !== 'gig') return;
	if (!TRANSPORT_COMMANDS.has(command.type)) return;
	const reason: RescueSnapshotReason = LOAD_COMMANDS.has(command.type) ? 'load' : 'transport';
	_pendingTransportReason = reason;
	if (_inFlight) _pendingTransportSnapshot = true;
	if (_transportDebounceTimer !== null) clearTimeout(_transportDebounceTimer);
	_transportDebounceTimer = setTimeout(() => {
		_transportDebounceTimer = null;
		if (_inFlight) {
			_pendingTransportSnapshot = true;
			return;
		}
		_postSnapshot(_pendingTransportReason, Date.now());
	}, RESCUE_TRANSPORT_DEBOUNCE_MS);
}

function _onPeriodicTick(now = Date.now()): void {
	if (
		!shouldPostPeriodicRescue(
			now,
			_lastPeriodicPostAt,
			_anyDeckPlaying(),
			uiPrefs.app_posture
		)
	) {
		return;
	}
	if (_inFlight) return;
	_postSnapshot('periodic', now);
}

export function installRescueRingWriterHooks(): void {
	_hooksInstalled = true;
}

export function uninstallRescueRingWriterHooks(): void {
	_hooksInstalled = false;
}

export interface RescueRingWriter {
	dispose(): void;
}

export function installRescueRingWriter(opts?: {
	now?: () => number;
	setInterval?: typeof globalThis.setInterval;
	clearInterval?: typeof globalThis.clearInterval;
}): RescueRingWriter {
	installRescueRingWriterHooks();
	const nowFn = opts?.now ?? (() => Date.now());
	const setIntervalFn = opts?.setInterval ?? globalThis.setInterval;
	const clearIntervalFn = opts?.clearInterval ?? globalThis.clearInterval;
	_intervalId = setIntervalFn(() => _onPeriodicTick(nowFn()), RESCUE_RING_PERIOD_MS);
	return {
		dispose: () => {
			if (_intervalId !== null) clearIntervalFn(_intervalId);
			_intervalId = null;
			if (_transportDebounceTimer !== null) clearTimeout(_transportDebounceTimer);
			_transportDebounceTimer = null;
			uninstallRescueRingWriterHooks();
		}
	};
}

/** Test seam: force a periodic evaluation without waiting for the interval. */
export function evaluateRescuePeriodicTick(now: number): void {
	_onPeriodicTick(now);
}

/** Test seam: expose transport command set. */
export function rescueTransportCommandTypes(): ReadonlySet<PerformanceCommand['type']> {
	return TRANSPORT_COMMANDS;
}

/** Test seam: reset module state between unit cases. */
export function resetRescueRingWriterForTest(): void {
	_lastSuccessfulSerialized = null;
	_lastPeriodicPostAt = 0;
	_inFlight = false;
	_pendingTransportSnapshot = false;
	if (_transportDebounceTimer !== null) clearTimeout(_transportDebounceTimer);
	_transportDebounceTimer = null;
}

/** Exported for restore tests; not used on the hot path. */
export { dispatchPerformanceCommand };
