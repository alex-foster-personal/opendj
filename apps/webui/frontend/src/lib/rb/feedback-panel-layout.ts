/**
 * Review-panel position, size, and first-open placement (FB-01, issue #11208).
 *
 * Split out of feedback.ts to stay under the 600-line file-size gate, the
 * same way feedback-pin-partial.ts and feedback-note.ts already did.
 */

import { clampToViewport } from '$lib/ui/clamp-to-viewport';

export interface PanelPos {
	x: number;
	y: number;
}

export const PANEL_POS_KEY = 'odj-feedback-panel-pos';

/** Keep at least this many px of the panel header reachable on both axes. */
export function clampPanelPos(
	pos: PanelPos,
	panel: { w: number; h: number },
	viewport: { w: number; h: number }
): PanelPos {
	const box = clampToViewport(
		pos.x,
		pos.y,
		{ width: panel.w, height: panel.h },
		{ width: viewport.w, height: viewport.h }
	);
	return { x: box.x, y: box.y };
}

export function parsePanelPos(raw: string | null): PanelPos | null {
	if (raw === null) return null;
	let parsed: unknown;
	try {
		parsed = JSON.parse(raw);
	} catch {
		return null;
	}
	if (typeof parsed !== 'object' || parsed === null) return null;
	const { x, y } = parsed as Record<string, unknown>;
	if (typeof x !== 'number' || typeof y !== 'number') return null;
	if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
	return { x, y };
}

export function serializePanelPos(pos: PanelPos): string {
	return JSON.stringify({ x: Math.round(pos.x), y: Math.round(pos.y) });
}

export interface PanelSize {
	w: number;
	h: number;
}

/** Persisted review-panel width and height (per-viewer convenience). */
export const PANEL_SIZE_KEY = 'odj-feedback-panel-size';

/** Legacy fixed width from the first FB-01 ship; guards must not regress to it. */
export const PANEL_LEGACY_FIXED_W = 272;

/** Default width is half the viewport (issue #11208). */
export const PANEL_DEFAULT_WIDTH_RATIO = 0.5;

export const PANEL_MIN_W = 220;
export const PANEL_MIN_H = 200;
export const PANEL_MAX_WIDTH_RATIO = 0.92;
export const PANEL_MAX_HEIGHT_RATIO = 0.85;
export const PANEL_DEFAULT_GAP_PX = 8;

/** Top edge used when no live topbar rect is available (matches first-ship y). */
export const PANEL_DEFAULT_TOP_PX = 34;

export interface PanelOpenAnchor {
	/** Left edge of the feedback thumbs cluster in viewport coordinates. */
	thumbsLeft: number;
	/** Top offset for the panel (typically just under the top bar). */
	top: number;
}

export function clampPanelSize(size: PanelSize, viewport: { w: number; h: number }): PanelSize {
	const maxW = Math.max(PANEL_MIN_W, Math.round(viewport.w * PANEL_MAX_WIDTH_RATIO));
	const maxH = Math.max(PANEL_MIN_H, Math.round(viewport.h * PANEL_MAX_HEIGHT_RATIO));
	return {
		w: Math.min(maxW, Math.max(PANEL_MIN_W, Math.round(size.w))),
		h: Math.min(maxH, Math.max(PANEL_MIN_H, Math.round(size.h)))
	};
}

/** First-open size: about 50% of the viewport, not the legacy 272px width. */
export function defaultPanelSize(viewport: { w: number; h: number }): PanelSize {
	const w = Math.round(viewport.w * PANEL_DEFAULT_WIDTH_RATIO);
	const h = Math.round(
		Math.min(viewport.h * PANEL_MAX_HEIGHT_RATIO, Math.max(300, viewport.h * 0.46))
	);
	return clampPanelSize({ w, h }, viewport);
}

/**
 * First-open position: the panel's right edge sits just LEFT of the thumbs
 * cluster (FB-07 moved the chevron cluster; the floating panel follows).
 */
export function defaultPanelPos(
	anchor: PanelOpenAnchor,
	panel: PanelSize,
	viewport: { w: number; h: number }
): PanelPos {
	const x = anchor.thumbsLeft - PANEL_DEFAULT_GAP_PX - panel.w;
	const y = anchor.top;
	return clampPanelPos({ x, y }, panel, viewport);
}

/** True when the panel's top-left opens left of the thumbs cluster (not past it). */
export function panelOpensLeftOfThumbs(pos: PanelPos, anchor: PanelOpenAnchor): boolean {
	return pos.x < anchor.thumbsLeft;
}

export function parsePanelSize(raw: string | null): PanelSize | null {
	if (raw === null) return null;
	let parsed: unknown;
	try {
		parsed = JSON.parse(raw);
	} catch {
		return null;
	}
	if (typeof parsed !== 'object' || parsed === null) return null;
	const { w, h } = parsed as Record<string, unknown>;
	if (typeof w !== 'number' || typeof h !== 'number') return null;
	if (!Number.isFinite(w) || !Number.isFinite(h)) return null;
	if (w <= 0 || h <= 0) return null;
	return { w, h };
}

export function serializePanelSize(size: PanelSize): string {
	return JSON.stringify({
		w: Math.round(size.w),
		h: Math.round(size.h)
	});
}
