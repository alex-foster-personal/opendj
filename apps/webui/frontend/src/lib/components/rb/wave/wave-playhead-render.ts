/**
 * Center playhead + BeatSyncMax deferred-seek overlays for wavestack rows.
 * Split from render.ts to keep file-size ratchet under the frontend ceiling.
 */
import type { AnlzBeat } from '$lib/rb/anlz-types';

export interface MasterDownbeatOverlay {
	masterBeats: readonly AnlzBeat[];
	masterPositionSec: number;
	followerPositionSec: number;
	masterPitch: number;
	followerPitch: number;
}

/** Canvas column of the playhead core. A fraction of 1 (the reference's exact
 * end in whole-track mode) lands on the first column past the canvas, which
 * is clipped, so the core clamps to the last drawable column. */
export function playheadCenterX(w: number, playheadFrac: number): number {
	return Math.min(Math.max(w - 1, 0), Math.max(0, Math.round(w * playheadFrac)));
}

/** The playhead: centered, or wherever the elastic zoom puts it (playheadFrac). Always drawn (busy waveforms + empty decks).
 * Beat Sync followers pass bar1 / synced / drift; others keep `now` (red).
 * Colours are supplied by the caller (render.ts owns PLAYHEAD_COLORS) rather
 * than imported, so this module never imports back from render.ts. */
export function drawPlayheadWithColors(
	ctx: CanvasRenderingContext2D,
	w: number,
	h: number,
	colors: Record<string, string>,
	tone: string = 'now',
	timeMs: number = 0,
	playheadFrac: number = 0.5
): void {
	const centerX = playheadCenterX(w, playheadFrac);
	if (tone === 'masterSynced') {
		_drawPlayheadGlow(ctx, colors.master, centerX - 1, h);
		_drawPlayheadGlow(ctx, colors.synced, centerX, h);
		_drawPlayheadCore(ctx, colors.master, centerX - 1, h);
		_drawPlayheadCore(ctx, colors.synced, centerX, h);
		return;
	}
	const color = colors[tone];
	let glow = 0.45;
	let core = 1;
	if (tone === 'drift') {
		const pulse = 0.55 + 0.45 * (0.5 + 0.5 * Math.sin(timeMs / 160));
		glow = 0.45 * pulse;
		core = pulse;
	}
	ctx.fillStyle = color;
	ctx.globalAlpha = glow;
	ctx.fillRect(centerX - 1, 0, 3, h);
	ctx.globalAlpha = core;
	ctx.fillRect(centerX, 0, 1, h);
	ctx.globalAlpha = 1;
}

export function drawGhostSeekPlayhead(
	ctx: CanvasRenderingContext2D,
	targetMs: number,
	tLeft: number,
	pxPerS: number,
	w: number,
	h: number
): void {
	const x = (targetMs / 1000 - tLeft) * pxPerS;
	if (x < -2 || x > w + 2) return;
	ctx.save();
	ctx.strokeStyle = 'rgba(255, 255, 255, 0.85)';
	ctx.lineWidth = 2;
	ctx.setLineDash([3, 3]);
	ctx.beginPath();
	ctx.moveTo(x, 0);
	ctx.lineTo(x, h);
	ctx.stroke();
	ctx.restore();
}

function _masterBeatX(
	beatT: number,
	overlay: MasterDownbeatOverlay,
	tLeft: number,
	pxPerS: number,
	w: number
): number | null {
	const { masterPositionSec, followerPositionSec, masterPitch, followerPitch } = overlay;
	if (masterPitch <= 0 || followerPitch <= 0) return null;
	const ratio = followerPitch / masterPitch;
	const followerT = followerPositionSec + (beatT - masterPositionSec) * ratio;
	const x = (followerT - tLeft) * pxPerS;
	if (x < -2 || x > w + 2) return null;
	return x;
}

export function drawMasterDownbeatOverlay(
	ctx: CanvasRenderingContext2D,
	overlay: MasterDownbeatOverlay,
	tLeft: number,
	pxPerS: number,
	w: number,
	h: number
): void {
	ctx.save();
	ctx.strokeStyle = 'rgba(255, 255, 255, 0.1)';
	ctx.lineWidth = 1;
	for (const beat of overlay.masterBeats) {
		if (beat.n !== 1) continue;
		const x = _masterBeatX(beat.t, overlay, tLeft, pxPerS, w);
		if (x === null) continue;
		ctx.beginPath();
		ctx.moveTo(x, 0);
		ctx.lineTo(x, h);
		ctx.stroke();
	}
	ctx.restore();
}

/** The master's beat grid drawn very faint across another deck row, so the
 * master's pulse reads as a click track through the lanes. Downbeats are a
 * little stronger than the off-beats. `includeDownbeats` is false when the
 * Beat Sync Max overlay is about to paint those lines itself. */
export function drawMasterGridGhost(
	ctx: CanvasRenderingContext2D,
	overlay: MasterDownbeatOverlay,
	tLeft: number,
	pxPerS: number,
	w: number,
	h: number,
	includeDownbeats: boolean
): void {
	ctx.save();
	ctx.lineWidth = 1;
	for (const beat of overlay.masterBeats) {
		const downbeat = beat.n === 1;
		if (downbeat && !includeDownbeats) continue;
		const x = _masterBeatX(beat.t, overlay, tLeft, pxPerS, w);
		if (x === null) continue;
		ctx.strokeStyle = downbeat ? 'rgba(255, 255, 255, 0.16)' : 'rgba(255, 255, 255, 0.07)';
		ctx.beginPath();
		ctx.moveTo(x, 0);
		ctx.lineTo(x, h);
		ctx.stroke();
	}
	ctx.restore();
}

function _drawPlayheadGlow(
	ctx: CanvasRenderingContext2D,
	color: string,
	coreX: number,
	h: number
): void {
	ctx.fillStyle = color;
	ctx.globalAlpha = 0.45;
	ctx.fillRect(coreX - 1, 0, 3, h);
	ctx.globalAlpha = 1;
}

function _drawPlayheadCore(
	ctx: CanvasRenderingContext2D,
	color: string,
	coreX: number,
	h: number
): void {
	ctx.fillStyle = color;
	ctx.globalAlpha = 1;
	ctx.fillRect(coreX, 0, 1, h);
	ctx.globalAlpha = 1;
}
