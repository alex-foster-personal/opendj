/**
 * Scrolling-waveform seek geometry and latest-only command dispatch.
 *
 * Requirements:
 *   ✔︎ A click seeks to the real track time beneath the pointer.
 *     [if] the pointer is 75% across a 24s window centered at 60s [then] target is 66s
 *   ✔︎ A drag behaves like grabbing the waveform under a fixed playhead.
 *     [if] a 400px-wide waveform moves right by 100px [then] audio moves earlier by 6s
 *   ✔︎ A release target cannot disappear behind an in-flight seek.
 *     [if] movement and release arrive during a pending seek [then] the final release is dispatched
 *   ✔︎ ✅ 🎯 WAVE-SEEK-01: a click lands on the spot under the pointer, like a CDJ needle
 *     search; with the deck's quantize on it snaps to the NEAREST beat, never a bar or phrase.
 *     [if] quantize is off and no modifier is held [then] the target is the raw position
 *     [if] quantize is on [then] the target is a real beat within half a beat of the click
 *     [if] the deck's quantize grid is 2 bars or phrase [then] the seek still lands within half a beat (sent exact)
 *   ✔︎ Shift restores exact click-anywhere behaviour.
 *     [if] shiftKey is held [then] the target is the raw, unsnapped position
 *   ✔︎ Cmd+Shift snaps to the nearest individual beat, quantize on or off.
 *     [if] shiftKey and metaKey are both held [then] the target lands on any real beat
 */

import {
	positionWithinGridSpan,
	quantizeToNearestBeat,
	quantizeToNearestDownbeat,
	validateBeatGrid
} from '$lib/rb/beat-sync-math';
import type { AnlzBeat } from '$lib/rb/anlz-types';
import { playheadCenterX } from './wave-playhead-render';

/** 'beat': nearest real beat. 'exact': the raw, unsnapped position.
 * 'downbeat': nearest bar downbeat, kept for snapWaveTargetMs callers; a
 * waveform click no longer picks it (WAVE-SEEK-01). */
export type WaveSnapMode = 'downbeat' | 'beat' | 'exact';

/** WAVE-SEEK-01 (supersedes pin a705aebfbeae's downbeat default): a plain
 * click follows the deck's quantize, snapping to the NEAREST beat only when it
 * is on, as rekordbox and a CDJ do. Cmd+Shift takes priority over plain Shift
 * so a DJ never loses the "snap to individual beats" reading by also holding
 * the plain-Shift modifier. */
export function waveSnapModeFromModifiers(
	modifiers: { shiftKey: boolean; metaKey: boolean },
	quantizeOn: boolean
): WaveSnapMode {
	if (modifiers.shiftKey && modifiers.metaKey) return 'beat';
	if (modifiers.shiftKey) return 'exact';
	return quantizeOn ? 'beat' : 'exact';
}

/** The engine command for a waveform seek. The target is already snapped here,
 * so it goes as exact: the deck's own quantize grid (1, 4, 8 beats or phrase)
 * would otherwise re-snap it up to a phrase away (WAVE-SEEK-01). Same `seek`
 * verb an agent drives through the performance IPC. */
export function waveSeekCommand<D>(
	deck: D,
	positionMs: number
): { type: 'seek'; deck: D; position_ms: number; exact: true } {
	return { type: 'seek', deck, position_ms: positionMs, exact: true };
}

/**
 * Snap a raw scrub target to the requested grid line. A missing/empty
 * beatgrid, or a grid this analysis's snap mode cannot resolve (e.g. no
 * downbeat detected), falls back to the raw target rather than breaking the
 * scrubber - a DJ without beatgrid data still gets ordinary click-anywhere
 * seeking, exactly today's behaviour.
 */
export function snapWaveTargetMs(
	targetMs: number,
	beats: readonly AnlzBeat[] | null | undefined,
	mode: WaveSnapMode
): number {
	if (mode === 'exact' || beats === null || beats === undefined) return targetMs;
	try {
		validateBeatGrid(beats);
	} catch {
		return targetMs;
	}
	// Off the grid there is no line to snap to (SEEK-GRID-01, #5601).
	if (!positionWithinGridSpan(beats, targetMs / 1000)) return targetMs;
	try {
		const snappedSec =
			mode === 'beat'
				? quantizeToNearestBeat(beats, targetMs / 1000)
				: quantizeToNearestDownbeat(beats, targetMs / 1000);
		return snappedSec * 1000;
	} catch {
		return targetMs;
	}
}

interface WaveClickTarget {
	centerPositionMs: number;
	durationMs: number;
	pointerX: number;
	widthPx: number;
	windowSeconds: number;
	/** Where the playhead sits across the row, 0..1 (WAVEZOOM-6802c). Required: the centered place is 0.5. */
	playheadFrac: number;
}

interface WaveDragTarget {
	clientX: number;
	durationMs: number;
	originClientX: number;
	originPositionMs: number;
	widthPx: number;
	windowSeconds: number;
}

export interface LatestSeekDispatcher {
	readonly pending: boolean;
	/** `pressT0Ms` is set only on the gesture target that should record seek-presented latency. */
	request(positionMs: number, pressT0Ms?: number): Promise<void>;
}

function _assertFinite(name: string, value: number): void {
	if (!Number.isFinite(value)) throw new RangeError(`${name} must be finite, got ${value}`);
}

function _assertPositive(name: string, value: number): void {
	if (!Number.isFinite(value) || value <= 0) {
		throw new RangeError(`${name} must be finite and positive, got ${value}`);
	}
}

function _assertGeometry(widthPx: number, durationMs: number, windowSeconds: number): void {
	_assertPositive('widthPx', widthPx);
	_assertPositive('durationMs', durationMs);
	_assertPositive('windowSeconds', windowSeconds);
}

function _clampToTrack(positionMs: number, durationMs: number): number {
	return Math.min(durationMs, Math.max(0, positionMs));
}

export function waveClickTargetMs(target: WaveClickTarget): number {
	_assertGeometry(target.widthPx, target.durationMs, target.windowSeconds);
	_assertFinite('centerPositionMs', target.centerPositionMs);
	_assertFinite('pointerX', target.pointerX);
	const pixelsPerSecond = target.widthPx / target.windowSeconds;
	// The same drawable column the playhead core is painted on, so a click on the
	// visible core seeks to the place it shows, including the exact end.
	if (!Number.isFinite(target.playheadFrac) || target.playheadFrac < 0 || target.playheadFrac > 1) {
		throw new RangeError(
			`waveClickTargetMs: playheadFrac must be within 0..1, got ${target.playheadFrac}`
		);
	}
	const playheadX = playheadCenterX(target.widthPx, target.playheadFrac);
	const offsetMs = ((target.pointerX - playheadX) / pixelsPerSecond) * 1000;
	return _clampToTrack(target.centerPositionMs + offsetMs, target.durationMs);
}

type WaveSnapInput = { beats: readonly AnlzBeat[] | null | undefined; mode: WaveSnapMode };

/** Zoomed waveform click: the time under the pointer, then the snap. */
export function waveClickSeekMs(target: WaveClickTarget & WaveSnapInput): number {
	return snapWaveTargetMs(waveClickTargetMs(target), target.beats, target.mode);
}

/** Overview strip click: proportional to the whole track, then the snap. */
export function overviewSeekMs(
	target: { pointerX: number; widthPx: number; durationMs: number } & WaveSnapInput
): number {
	_assertPositive('widthPx', target.widthPx);
	_assertPositive('durationMs', target.durationMs);
	_assertFinite('pointerX', target.pointerX);
	const ratio = Math.min(1, Math.max(0, target.pointerX / target.widthPx));
	return snapWaveTargetMs(ratio * target.durationMs, target.beats, target.mode);
}

export function waveDragTargetMs(target: WaveDragTarget): number {
	_assertGeometry(target.widthPx, target.durationMs, target.windowSeconds);
	_assertFinite('originPositionMs', target.originPositionMs);
	_assertFinite('originClientX', target.originClientX);
	_assertFinite('clientX', target.clientX);
	const pixelsPerSecond = target.widthPx / target.windowSeconds;
	const draggedMs = ((target.clientX - target.originClientX) / pixelsPerSecond) * 1000;
	return _clampToTrack(target.originPositionMs - draggedMs, target.durationMs);
}

type SeekDispatch = (positionMs: number, pressT0Ms?: number) => Promise<void>;

class _LatestSeekDispatcher implements LatestSeekDispatcher {
	private drainPromise: Promise<void> | null = null;
	private queued: { positionMs: number; pressT0Ms?: number } | null = null;

	constructor(private readonly dispatch: SeekDispatch) {}

	get pending(): boolean {
		return this.drainPromise !== null || this.queued !== null;
	}

	request(positionMs: number, pressT0Ms?: number): Promise<void> {
		if (!Number.isFinite(positionMs) || positionMs < 0) {
			throw new RangeError(`positionMs must be finite and non-negative, got ${positionMs}`);
		}
		if (pressT0Ms !== undefined && (!Number.isFinite(pressT0Ms) || pressT0Ms < 0)) {
			throw new RangeError(
				`pressT0Ms must be finite and non-negative when provided, got ${pressT0Ms}`
			);
		}
		this.queued = { positionMs, ...(pressT0Ms === undefined ? {} : { pressT0Ms }) };
		if (this.drainPromise === null) {
			const drain = this._drain();
			const tracked = drain.finally(() => {
				if (this.drainPromise === tracked) this.drainPromise = null;
			});
			this.drainPromise = tracked;
		}
		return this.drainPromise;
	}

	private async _drain(): Promise<void> {
		try {
			while (this.queued !== null) {
				const { positionMs, pressT0Ms } = this.queued;
				this.queued = null;
				await this.dispatch(positionMs, pressT0Ms);
			}
		} catch (error) {
			this.queued = null;
			throw error;
		}
	}
}

export function createLatestSeekDispatcher(dispatch: SeekDispatch): LatestSeekDispatcher {
	return new _LatestSeekDispatcher(dispatch);
}
