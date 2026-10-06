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
 *   ✔︎ A scrub target snaps to the nearest downbeat by default (pin a705aebfbeae).
 *     [if] no modifier is held and a beatgrid is loaded [then] the target lands on a real downbeat
 *   ✔︎ Shift restores exact click-anywhere behaviour.
 *     [if] shiftKey is held [then] the target is the raw, unsnapped position
 *   ✔︎ Cmd+Shift snaps to the nearest individual beat instead of a downbeat.
 *     [if] shiftKey and metaKey are both held [then] the target lands on any real beat
 */

import {
	positionWithinGridSpan,
	quantizeToNearestBeat,
	quantizeToNearestDownbeat,
	validateBeatGrid
} from '$lib/rb/beat-sync-math';
import type { AnlzBeat } from '$lib/rb/anlz-types';

/** 'downbeat' (default): nearest bar downbeat. 'beat': nearest real beat
 * (Cmd+Shift). 'exact': the raw, unsnapped position (Shift alone). */
export type WaveSnapMode = 'downbeat' | 'beat' | 'exact';

/** Cmd+Shift takes priority over plain Shift so a DJ never loses the "snap
 * to individual beats" reading by also holding the plain-Shift modifier. */
export function waveSnapModeFromModifiers(modifiers: {
	shiftKey: boolean;
	metaKey: boolean;
}): WaveSnapMode {
	if (modifiers.shiftKey && modifiers.metaKey) return 'beat';
	if (modifiers.shiftKey) return 'exact';
	return 'downbeat';
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
	request(positionMs: number): Promise<void>;
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
	const offsetMs = ((target.pointerX - target.widthPx / 2) / pixelsPerSecond) * 1000;
	return _clampToTrack(target.centerPositionMs + offsetMs, target.durationMs);
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

class _LatestSeekDispatcher implements LatestSeekDispatcher {
	private drainPromise: Promise<void> | null = null;
	private queuedPositionMs: number | null = null;

	constructor(private readonly dispatch: (positionMs: number) => Promise<void>) {}

	get pending(): boolean {
		return this.drainPromise !== null || this.queuedPositionMs !== null;
	}

	request(positionMs: number): Promise<void> {
		if (!Number.isFinite(positionMs) || positionMs < 0) {
			throw new RangeError(`positionMs must be finite and non-negative, got ${positionMs}`);
		}
		this.queuedPositionMs = positionMs;
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
			while (this.queuedPositionMs !== null) {
				const positionMs = this.queuedPositionMs;
				this.queuedPositionMs = null;
				await this.dispatch(positionMs);
			}
		} catch (error) {
			this.queuedPositionMs = null;
			throw error;
		}
	}
}

export function createLatestSeekDispatcher(
	dispatch: (positionMs: number) => Promise<void>
): LatestSeekDispatcher {
	return new _LatestSeekDispatcher(dispatch);
}
