/**
 * Split main waveform (skin preview, pref `wave_split_master`): one deck row
 * paints a PARTNER deck in its top half and this deck in its bottom half,
 * meeting at the centerline. Each half is a full real drawWaveRow frame at
 * half height (own position, own pitch warp, own grid and cues), so in-phase
 * beats line up across the line and drift reads as a horizontal offset.
 * Ported from the split-wave mockup (commit 0dbeca081d).
 *
 * Requirements (mini-PRD):
 *   ✔︎ splitPartnerDeck: follower -> master deck; master (or no master
 *     elected) -> next loaded deck after this one, wrapping; nothing else
 *     loaded -> null (row stays one-sided).
 *     [if] a follower's partner is not the master [then ⛔️]
 *     [if] the master's partner is itself or an empty deck [then ⛔️]
 *     [if] a lone loaded deck gets a partner [then ⛔️]
 *   ✔︎ paintSplitRow: partner half on top (bars grow up to the centerline),
 *     this deck flipped into the bottom half (bars grow down from it).
 */
import type { DeckId } from '$lib/rb/deck-id';
import { drawWaveRow, type WaveRowFrame } from './render';

export interface SplitDeckRef {
	id: DeckId;
	loaded: boolean;
	isMaster: boolean;
}

export interface SplitPartner {
	id: DeckId;
	label: string;
}

export function splitPartnerDeck(selfId: DeckId, decks: readonly SplitDeckRef[]): SplitPartner | null {
	const master = decks.find((d) => d.isMaster && d.loaded) ?? null;
	if (master !== null && master.id !== selfId) return { id: master.id, label: `MASTER ${master.id}` };
	const start = decks.findIndex((d) => d.id === selfId);
	if (start < 0) throw new Error(`split row: deck ${selfId} not in deck list`);
	for (let step = 1; step < decks.length; step++) {
		const candidate = decks[(start + step) % decks.length];
		if (candidate.loaded) return { id: candidate.id, label: `DECK ${candidate.id}` };
	}
	return null;
}

const _scratch = new WeakMap<HTMLCanvasElement, { top: HTMLCanvasElement; bottom: HTMLCanvasElement }>();

function _paintHalf(target: HTMLCanvasElement, frame: WaveRowFrame, dpr: number): void {
	target.width = Math.round(frame.widthCss * dpr);
	target.height = Math.round(frame.heightCss * dpr);
	const ctx = target.getContext('2d');
	if (ctx === null) throw new Error('split row: 2d context unavailable');
	ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
	drawWaveRow(ctx, frame);
}

/** `top` and `bottom` are frames at heightCss = row height / 2. */
export function paintSplitRow(
	row: HTMLCanvasElement,
	ctx: CanvasRenderingContext2D,
	top: WaveRowFrame,
	bottom: WaveRowFrame,
	labels: { top: string; bottom: string; color: string; line: string },
	dpr: number
): void {
	let scratch = _scratch.get(row);
	if (scratch === undefined) {
		scratch = { top: document.createElement('canvas'), bottom: document.createElement('canvas') };
		_scratch.set(row, scratch);
	}
	_paintHalf(scratch.top, top, dpr);
	_paintHalf(scratch.bottom, bottom, dpr);
	const w = top.widthCss;
	const half = top.heightCss;
	ctx.save();
	ctx.setTransform(1, 0, 0, 1, 0, 0);
	ctx.drawImage(scratch.top, 0, 0);
	ctx.translate(0, Math.round(half * 2 * dpr));
	ctx.scale(1, -1);
	ctx.drawImage(scratch.bottom, 0, 0);
	ctx.restore();
	ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
	ctx.fillStyle = labels.line;
	ctx.fillRect(0, half - 0.5, w, 1);
	ctx.font = '8px ui-monospace, Menlo, monospace';
	ctx.fillStyle = labels.color;
	ctx.fillText(labels.top, 3, 9);
	ctx.fillText(labels.bottom, 3, half * 2 - 3);
}
