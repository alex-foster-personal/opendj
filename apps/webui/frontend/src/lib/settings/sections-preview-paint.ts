/**
 * Settings live preview for STRUCTUI segment display prefs (issue #11436).
 * Mock track layout only; colors are placeholders until skin tokens land.
 */
import type { SectionOverlayPrefsState } from '$lib/rb/section-overlay-prefs';

export const SECTIONS_PREVIEW_BACKGROUND = '#0a0c10';

const _DEMO: ReadonlyArray<{ label: string; start: number; color: string; uncertain?: boolean }> = [
	{ label: 'Intro', start: 0, color: '#5b8def' },
	{ label: 'Build', start: 0.22, color: '#e8a33c' },
	{ label: 'Drop', start: 0.42, color: '#e5484d' },
	{ label: 'Outro', start: 0.72, color: '#8a7ff0', uncertain: true }
];

export type SectionsPreviewPaintFn = typeof paintSectionsPreview;

function _visibleSections(prefs: SectionOverlayPrefsState) {
	return _DEMO.filter((s) => !(s.uncertain && prefs.sections_uncertain === 'hide'));
}

function _spanX(w: number, start: number, end: number): number {
	return Math.max(0, (end - start) * w);
}

export function paintSectionsPreview(
	ctx: CanvasRenderingContext2D,
	w: number,
	h: number,
	prefs: SectionOverlayPrefsState
): void {
	ctx.clearRect(0, 0, w, h);
	ctx.fillStyle = SECTIONS_PREVIEW_BACKGROUND;
	ctx.fillRect(0, 0, w, h);
	if (!prefs.show_sections) {
		ctx.globalAlpha = 0.45;
		ctx.fillStyle = '#8a93a3';
		ctx.font = '600 11px system-ui, sans-serif';
		ctx.fillText('Show segments off', 8, h / 2);
		ctx.globalAlpha = 1;
		return;
	}
	const sections = _visibleSections(prefs);
	const wfY = Math.round(h * 0.55);
	const wfH = Math.round(h * 0.28);
	// Neutral tri-band mock waveform
	for (let x = 0; x < w; x += 2) {
		const t = x / w;
		const amp = 0.35 + 0.3 * Math.abs(Math.sin(t * 38));
		ctx.fillStyle = '#3a6fe0';
		ctx.fillRect(x, wfY - amp * wfH, 1.6, amp * wfH * 2);
		ctx.fillStyle = '#e8a33c';
		ctx.fillRect(x, wfY - amp * wfH * 0.65, 1.6, amp * wfH * 1.3);
		ctx.fillStyle = '#f2f4f8';
		ctx.fillRect(x, wfY - amp * wfH * 0.32, 1.6, amp * wfH * 0.64);
	}
	const style = prefs.sections_main_style;
	const playheadX = Math.round(w * 0.58);
	for (let i = 0; i < sections.length; i += 1) {
		const s = sections[i]!;
		const end = i + 1 < sections.length ? sections[i + 1]!.start : 1;
		const x0 = s.start * w;
		const sw = _spanX(w, s.start, end);
		const alpha = s.uncertain ? 0.28 : 1;
		ctx.globalAlpha = alpha;
		if (style === 'm1_tint' || style === 'm2_recolor') {
			ctx.globalAlpha *= style === 'm1_tint' ? 0.22 : 0.55;
			ctx.fillStyle = s.color;
			ctx.fillRect(x0, wfY - wfH, sw, wfH * 2);
			if (style === 'm2_recolor') {
				ctx.globalAlpha = alpha * 0.35;
				ctx.fillStyle = '#ffffff';
				ctx.fillRect(x0, wfY - wfH * 0.2, sw, wfH * 0.4);
			}
		} else if (style === 'm3_lines') {
			ctx.strokeStyle = s.color;
			ctx.lineWidth = 2;
			ctx.beginPath();
			ctx.moveTo(x0, wfY - wfH);
			ctx.lineTo(x0, wfY + wfH);
			ctx.stroke();
			ctx.fillStyle = s.color;
			ctx.font = '600 9px system-ui, sans-serif';
			ctx.fillText(s.label.slice(0, 3).toUpperCase(), x0 + 2, wfY - wfH + 10);
		} else if (style === 'm4_base') {
			const baseY = wfY + wfH + 4;
			ctx.fillStyle = s.color;
			ctx.fillRect(x0, baseY, sw, 4);
			ctx.font = '600 9px system-ui, sans-serif';
			ctx.fillText(s.label.slice(0, 4).toUpperCase(), x0 + 2, baseY - 2);
		}
		const atBoundary = Math.abs(x0 - playheadX) < 4;
		if (!atBoundary && x0 > 4 && x0 < w - 20) {
			ctx.fillStyle = s.color;
			ctx.globalAlpha = alpha;
			ctx.font = '600 9px system-ui, sans-serif';
			const tag = s.label.toUpperCase() + (s.uncertain ? ' ?' : '');
			ctx.fillText(tag, x0 + 2, wfY - wfH - 4);
		}
		ctx.globalAlpha = 1;
	}
	if (prefs.sections_sticky_title) {
		const under = sections.find((s, i) => {
			const end = i + 1 < sections.length ? sections[i + 1]!.start : 1;
			const x0 = s.start * w;
			const x1 = end * w;
			return playheadX >= x0 && playheadX < x1;
		});
		if (under !== undefined) {
			ctx.fillStyle = 'rgba(11,13,17,.85)';
			ctx.fillRect(2, 4, 54, 16);
			ctx.fillStyle = under.color;
			ctx.font = '600 10px system-ui, sans-serif';
			ctx.fillText(under.label.toUpperCase(), 6, 16);
		}
	}
	if (prefs.sections_next_countdown) {
		ctx.fillStyle = '#e7eaf0';
		ctx.font = '600 9px system-ui, sans-serif';
		ctx.fillText('DROP in 8 bars', playheadX + 6, 14);
	}
	ctx.fillStyle = '#e7eaf0';
	ctx.fillRect(playheadX - 1, 0, 2, h);
	// Overview strip mock
	const ovY = Math.round(h * 0.12);
	const ovH = Math.round(h * 0.14);
	ctx.fillStyle = '#1b2030';
	ctx.fillRect(0, ovY, w, ovH);
	const ovStyle = prefs.sections_overview_style;
	for (let i = 0; i < sections.length; i += 1) {
		const s = sections[i]!;
		const end = i + 1 < sections.length ? sections[i + 1]!.start : 1;
		const x0 = s.start * w;
		const sw = _spanX(w, s.start, end);
		ctx.globalAlpha = s.uncertain ? 0.35 : 1;
		if (ovStyle === 'o1_strip') {
			ctx.fillStyle = s.color;
			ctx.fillRect(x0, ovY - 6, sw, 5);
			ctx.strokeStyle =
				playheadX >= x0 && playheadX < x0 + sw ? '#ffffff' : 'rgba(255,255,255,.25)';
			ctx.strokeRect(x0, ovY - 6, sw, 5);
		} else if (ovStyle === 'o2_tint') {
			ctx.fillStyle = s.color;
			ctx.globalAlpha *= 0.18;
			ctx.fillRect(x0, ovY, sw, ovH);
		} else {
			ctx.fillStyle = s.color;
			ctx.globalAlpha *= 0.75;
			ctx.fillRect(x0, ovY + 2, sw, ovH - 4);
		}
		ctx.globalAlpha = 1;
	}
	// Library mock
	const libY = h - Math.round(h * 0.22);
	if (prefs.sections_library_structure_column) {
		let cx = 6;
		for (const s of sections) {
			ctx.fillStyle = s.color;
			ctx.fillRect(cx, libY, 14, 10);
			cx += 16;
		}
		ctx.fillStyle = '#8a93a3';
		ctx.font = '500 9px system-ui, sans-serif';
		ctx.fillText('16 bars to drop', cx + 4, libY + 9);
	}
	if (prefs.sections_preview_cursor_label) {
		ctx.fillStyle = '#3fb68b';
		ctx.fillRect(w * 0.35, libY + 14, 40, 2);
		ctx.fillStyle = '#e7eaf0';
		ctx.fillText('Build · bar 48', w * 0.35, libY + 28);
	}
	if (prefs.sections_uncertain === 'show_faded') {
		ctx.setLineDash([3, 3]);
		ctx.strokeStyle = 'rgba(255,255,255,.35)';
		ctx.strokeRect(4, 2, w - 8, h - 4);
		ctx.setLineDash([]);
	}
}
