/**
 * Center playhead + BeatSyncMax deferred-seek overlays for wavestack rows.
 * Split from render.ts to keep file-size ratchet under the frontend ceiling.
 */
import type { AnlzBeat } from '$lib/rb/anlz-types';

const PLAYHEAD_COLORS = {
	stopped: '#fff',
	now: '#e23a32',
	master: '#e0cc6e',
	bar1: '#35c04f',
	synced: '#7ed992',
	drift: '#ff2d2d'
} as const;

export type PlayheadTone = keyof typeof PLAYHEAD_COLORS | 'masterSynced';

export interface MasterDownbeatOverlay {
	masterBeats: readonly AnlzBeat[];
	masterPositionSec: number;
	followerPositionSec: number;
	masterPitch: number;
	followerPitch: number;
}

/** Fixed center playhead. Always drawn (busy waveforms + empty decks).
 * Beat Sync followers pass bar1 / synced / drift; others keep `now` (red). */
export function drawPlayhead(
	ctx: CanvasRenderingContext2D,
	w: number,
	h: number,
	tone: PlayheadTone = 'now',
	timeMs: number = 0
): void {
	const centerX = Math.round(w / 2);
	if (tone === 'masterSynced') {
		_drawPlayheadGlow(ctx, PLAYHEAD_COLORS.master, centerX - 1, h);
		_drawPlayheadGlow(ctx, PLAYHEAD_COLORS.synced, centerX, h);
		_drawPlayheadCore(ctx, PLAYHEAD_COLORS.master, centerX - 1, h);
		_drawPlayheadCore(ctx, PLAYHEAD_COLORS.synced, centerX, h);
		return;
	}
	const color = PLAYHEAD_COLORS[tone];
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

export function drawMasterDownbeatOverlay(
	ctx: CanvasRenderingContext2D,
	overlay: MasterDownbeatOverlay,
	tLeft: number,
	pxPerS: number,
	w: number,
	h: number
): void {
	const { masterBeats, masterPositionSec, followerPositionSec, masterPitch, followerPitch } = overlay;
	if (masterPitch <= 0 || followerPitch <= 0) return;
	const ratio = followerPitch / masterPitch;
	ctx.save();
	ctx.strokeStyle = 'rgba(255, 255, 255, 0.1)';
	ctx.lineWidth = 1;
	for (const beat of masterBeats) {
		if (beat.n !== 1) continue;
		const delta = beat.t - masterPositionSec;
		const followerT = followerPositionSec + delta * ratio;
		const x = (followerT - tLeft) * pxPerS;
		if (x < -2 || x > w + 2) continue;
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
