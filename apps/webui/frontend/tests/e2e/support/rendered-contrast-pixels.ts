/**
 * Pure backdrop-pixel maths for the rendered contrast audit (#10368): no
 * imports, so the unit test loads it without Playwright or pngjs.
 */

export interface RgbaImage {
	width: number;
	height: number;
	data: Uint8Array;
}

export interface CssBox {
	x: number;
	y: number;
	width: number;
	height: number;
}

export type BackdropVia = 'svg' | 'pixel';

export type BackdropSample =
	{ ratio: number; bg: number[] } | { why: 'zero-sized' | 'off-screen' | 'translucent pixels' };

function _linear(c: number): number {
	const v = c / 255;
	return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
}

function _luminance(rgb: readonly number[]): number {
	return 0.2126 * _linear(rgb[0]) + 0.7152 * _linear(rgb[1]) + 0.0722 * _linear(rgb[2]);
}

/** WCAG 2 contrast ratio of two sRGB colours (0-255 channels). */
export function contrastRatio(a: readonly number[], b: readonly number[]): number {
	const la = _luminance(a);
	const lb = _luminance(b);
	return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

/** WCAG 1.4.3 floor: 3:1 for large text (24px, or 18.66px bold), else 4.5:1. */
export function requiredRatio(size: number, bold: boolean): number {
	return size >= 24 || (size >= 18.66 && bold) ? 3 : 4.5;
}

/** A 3 by 3 grid plus the four edge midpoints of a `width` x `height` pixel box, de-duplicated. */
export function backdropSamplePoints(width: number, height: number): { x: number; y: number }[] {
	const clamp = (v: number, n: number) => Math.min(n - 1, Math.max(0, Math.floor(v)));
	const xs = [width / 6, width / 2, (5 * width) / 6].map((v) => clamp(v, width));
	const ys = [height / 6, height / 2, (5 * height) / 6].map((v) => clamp(v, height));
	const midX = clamp(width / 2, width);
	const midY = clamp(height / 2, height);
	const points = [
		...xs.flatMap((x) => ys.map((y) => ({ x, y }))),
		{ x: 0, y: midY },
		{ x: width - 1, y: midY },
		{ x: midX, y: 0 },
		{ x: midX, y: height - 1 }
	];
	const seen = new Set<string>();
	return points.filter((p) => {
		const k = `${p.x},${p.y}`;
		if (seen.has(k)) return false;
		seen.add(k);
		return true;
	});
}

/**
 * The worst contrast of `fg` against the backdrop pixels under `box` (CSS px
 * at device scale 1). `svg` reads every pixel (a drawn knob fails on any
 * clashing stroke); `pixel` reads the grid plus edges, so one waveform peak
 * pixel does not decide the result. Only the on-image part of the box is read.
 */
export function worstBackdropRatio(
	image: RgbaImage,
	box: CssBox,
	fg: readonly number[],
	via: BackdropVia
): BackdropSample {
	if (box.width < 1 || box.height < 1) return { why: 'zero-sized' };
	const x0 = Math.max(0, Math.floor(box.x));
	const y0 = Math.max(0, Math.floor(box.y));
	const x1 = Math.min(image.width, Math.ceil(box.x + box.width));
	const y1 = Math.min(image.height, Math.ceil(box.y + box.height));
	if (x1 <= x0 || y1 <= y0) return { why: 'off-screen' };
	const points: { x: number; y: number }[] = [];
	if (via === 'svg') {
		for (let y = y0; y < y1; y++) for (let x = x0; x < x1; x++) points.push({ x, y });
	} else if (via === 'pixel') {
		for (const p of backdropSamplePoints(x1 - x0, y1 - y0))
			points.push({ x: x0 + p.x, y: y0 + p.y });
	}
	let worst: { ratio: number; bg: number[] } | null = null;
	for (const { x, y } of points) {
		const i = (y * image.width + x) * 4;
		if (image.data[i + 3] < 255) return { why: 'translucent pixels' };
		const bg = [image.data[i], image.data[i + 1], image.data[i + 2]];
		const ratio = contrastRatio(fg, bg);
		if (!worst || ratio < worst.ratio) worst = { ratio, bg };
	}
	return worst!;
}
