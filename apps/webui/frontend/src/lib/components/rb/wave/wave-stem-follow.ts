/** Main-row paint choice for the wave_stem_follow pref (rekordbox ACTIVE STEM waveform, mimicked). */

import { drawStemWaveRow } from './render';
import type { StemControl } from '$lib/rb/stem-types';

export type StemFollowEntry =
	| { status: 'loading' }
	| { status: 'ready'; envelope: readonly number[] }
	| { status: 'error'; code: string }
	| undefined;

export interface StemFollowFrame {
	enabled: boolean;
	stem: StemControl | null;
	entry: StemFollowEntry;
	color: string;
	scrollPx: number;
	durationMs: number | null;
	pitch: number;
	width: number;
	height: number;
	windowS: number;
	playheadFrac: number;
}

/** The main row paint (drawWaveRow); `body` replaces the mix bands, everything over them still paints. */
export type MainRowPaint = (body?: (ctx: CanvasRenderingContext2D) => void) => void;

/**
 * Paint the sole audible stem's envelope as the main row's body when the pref is on and it is
 * ready, else the mix. Either way the row paint runs, so the beat grid, loop, cues, phrases and
 * playhead stay on the row while a stem is followed.
 */
export function paintMainWaveWithStemFollow(
	ctx: CanvasRenderingContext2D,
	frame: StemFollowFrame,
	paintMix: MainRowPaint
): 'stem' | 'mix' {
	const { enabled, stem, entry } = frame;
	if (
		!enabled ||
		stem === null ||
		entry === undefined ||
		entry.status !== 'ready' ||
		frame.color === ''
	) {
		paintMix();
		return 'mix';
	}
	const envelope = entry.envelope;
	paintMix((rowCtx) =>
		drawStemWaveRow(rowCtx, {
			envelope,
			scrollPx: frame.scrollPx,
			durationMs: frame.durationMs,
			pitch: frame.pitch,
			width: frame.width,
			height: frame.height,
			color: frame.color,
			windowS: frame.windowS,
			playheadFrac: frame.playheadFrac,
			alpha: 1,
			clear: false
		})
	);
	return 'stem';
}
