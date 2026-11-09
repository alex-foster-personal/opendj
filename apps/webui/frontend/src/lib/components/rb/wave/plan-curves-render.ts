/**
 * WAVEPLAN-7896, WAVEPLAN-7896f: a pending plan over a deck row. The span it covers is a loop-style band, red on the
 * outgoing deck and green on the incoming, and its knobs are the bench page's lines (solid fader, dashed EQs, dotted
 * filter) on a 0..1 scale from the row's bottom to its top, each over a dark halo so it reads on busy audio. Runs
 * inside the waveform's paint, so it allocates nothing: the geometry is built once per move (`plan-overlay.ts`).
 */
import type { WavePalette } from './cues';
import { PLAN_KNOBS, type PlanKnob, type PlanKnobColor } from '$lib/rb/plan-knobs';
import type { PlanCurve, PlanDeckOverlay } from '$lib/rb/plan-overlay';

const INSET_PX = 4;
const FADER_WIDTH_PX = 2.5;
const KNOB_WIDTH_PX = 2;
const HALO_EXTRA_PX = 2;
const HALO_COLOR = 'rgba(0, 0, 0, 0.4)';
const BAND_ALPHA = 0.14;
const EDGE_WIDTH_PX = 2;
const SOLID: number[] = [];
const KNOB_DASH = Object.fromEntries(PLAN_KNOBS.map((knob) => [knob.name, knob.dash])) as Record<
	PlanKnob,
	number[]
>;
const KNOB_COLOR = Object.fromEntries(PLAN_KNOBS.map((knob) => [knob.name, knob.color])) as Record<
	PlanKnob,
	PlanKnobColor
>;

function _traceCurve(
	ctx: CanvasRenderingContext2D,
	curve: PlanCurve,
	tLeft: number,
	pxPerS: number,
	band: number
): void {
	ctx.beginPath();
	for (let p = 0; p < curve.points.length; p++) {
		const point = curve.points[p];
		const x = (point[0] / 1000 - tLeft) * pxPerS;
		const y = INSET_PX + (1 - point[1]) * band;
		if (p === 0) ctx.moveTo(x, y);
		else ctx.lineTo(x, y);
	}
}

function _drawSpan(
	ctx: CanvasRenderingContext2D,
	color: string,
	startX: number,
	endX: number,
	h: number
): void {
	ctx.fillStyle = color;
	ctx.globalAlpha = BAND_ALPHA;
	ctx.fillRect(startX, 0, endX - startX, h);
	ctx.globalAlpha = 1;
	ctx.fillRect(startX, 0, EDGE_WIDTH_PX, h);
	ctx.fillRect(endX - EDGE_WIDTH_PX, 0, EDGE_WIDTH_PX, h);
}

export function drawPlanCurves(
	ctx: CanvasRenderingContext2D,
	overlay: PlanDeckOverlay,
	palette: WavePalette,
	tLeft: number,
	pxPerS: number,
	w: number,
	h: number
): void {
	if (overlay.start_ms === null || overlay.end_ms === null) return;
	const startX = (overlay.start_ms / 1000 - tLeft) * pxPerS;
	const endX = (overlay.end_ms / 1000 - tLeft) * pxPerS;
	if (endX < 0 || startX > w) return;
	_drawSpan(ctx, overlay.role === 'clock' ? palette.planOut : palette.planIn, startX, endX, h);
	const band = h - INSET_PX * 2;
	// Last to first, so the fader lands on top.
	for (let c = overlay.curves.length - 1; c >= 0; c--) {
		const curve = overlay.curves[c];
		const width = curve.name === 'fader' ? FADER_WIDTH_PX : KNOB_WIDTH_PX;
		_traceCurve(ctx, curve, tLeft, pxPerS, band);
		// Solid: a dashed stroke costs several times a solid one, and an unbroken underlay carries the dashes too.
		ctx.setLineDash(SOLID);
		ctx.strokeStyle = HALO_COLOR;
		ctx.lineWidth = width + HALO_EXTRA_PX;
		ctx.stroke();
		ctx.setLineDash(KNOB_DASH[curve.name]);
		ctx.strokeStyle = palette[KNOB_COLOR[curve.name]];
		ctx.lineWidth = width;
		ctx.stroke();
	}
	ctx.setLineDash(SOLID);
}
