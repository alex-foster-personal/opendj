import type { Page } from '@playwright/test';
import { PNG } from 'pngjs';

import { requiredRatio, worstBackdropRatio, type BackdropVia } from './rendered-contrast-pixels';

export { backdropSamplePoints, contrastRatio, requiredRatio, worstBackdropRatio } from './rendered-contrast-pixels';

/**
 * Rendered contrast audit (UICONTRAST-01, orders board W34).
 *
 * The token-pair validator (src/lib/rb/color-contrast.ts) measures the colors
 * a skin DECLARES. This measures what the browser actually PAINTS: every
 * visible text element's computed color against the backdrop it renders on,
 * with translucent fills composited down through their ancestors, so a
 * `rgba(255,255,255,0.1)` row tint is measured as the gray it is rather than
 * flagged as white.
 *
 * `renderedContrastAudit` is passed to `page.evaluate`, so it must stay
 * self-contained: it never references this module's imports or module scope
 * (the Node-side sampler below is not serialized).
 */

export interface ContrastFinding {
	/** CSS-ish path from the nearest stable identity ancestor through intervening classed ancestors to the leaf `tag.own.classes`. */
	el: string;
	text: string;
	fg: string;
	bg: string;
	ratio: number;
	need: number;
	/** What measured it: the in-page CSS compositor, or the backdrop pixel sampler (`svg` every pixel, `pixel` grid). */
	via: 'css' | BackdropVia;
}

export interface UnmeasuredElement {
	el: string;
	why: string;
	/** Set when the backdrop has pixels but no CSS colour: `sampleUnmeasuredBackdrops`
	 * measures the text against the rendered pixels, found by `data-contrast-backdrop`. */
	backdrop?: {
		id: string;
		via: BackdropVia;
		text: string;
		fg: number[];
		size: number;
		bold: boolean;
		probe: boolean;
		box: 'contents' | 'element';
	};
}

export interface RenderedContrastReport {
	measured: number;
	findings: ContrastFinding[];
	unmeasured: UnmeasuredElement[];
	/** Every element carrying data-contrast-probe, pass or fail (controls). */
	probes: ContrastFinding[];
}

/**
 * Init script (`page.addInitScript(trackCanvasText)`): remember every canvas
 * that paints text with fillText/strokeText. Canvas text has no DOM node or
 * CSS color for the audit to read, so it must be reported, never skipped.
 * Text painted on an OffscreenCanvas in a worker is not seen.
 */
export function trackCanvasText(): void {
	const painted = new WeakSet<HTMLCanvasElement>();
	(window as unknown as { __contrastCanvasText?: WeakSet<HTMLCanvasElement> }).__contrastCanvasText = painted;
	const proto = CanvasRenderingContext2D.prototype;
	for (const m of ['fillText', 'strokeText'] as const) {
		const original = proto[m];
		proto[m] = function (this: CanvasRenderingContext2D, ...args: Parameters<typeof original>) {
			painted.add(this.canvas);
			return original.apply(this, args);
		};
	}
}

/** `scope` is a selector: audit only that subtree (one panel), else the whole body. */
export function renderedContrastAudit(scope?: string): RenderedContrastReport {
	const canvas = document.createElement('canvas');
	canvas.width = 1;
	canvas.height = 1;
	const ctx = canvas.getContext('2d', { willReadFrequently: true });
	const parse = (s: string): number[] | null => {
		if (!s || s === 'transparent') return [0, 0, 0, 0];
		const m = s.match(/^rgba?\(([^)]+)\)$/);
		if (m) {
			const p = m[1].split(/[\s,/]+/).filter(Boolean).map(Number);
			return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
		}
		// oklab()/color-mix()/color(srgb ...) results: let the browser's own
		// color parser resolve them to sRGB through a 1px canvas.
		if (!ctx) return null;
		ctx.clearRect(0, 0, 1, 1);
		ctx.fillStyle = 'rgba(1, 2, 3, 0.004)';
		ctx.fillStyle = s;
		if (ctx.fillStyle === 'rgba(1, 2, 3, 0.004)') return null;
		ctx.fillRect(0, 0, 1, 1);
		const d = ctx.getImageData(0, 0, 1, 1).data;
		return [d[0], d[1], d[2], d[3] / 255];
	};
	const over = (top: number[], bot: number[]): number[] => {
		const a = top[3] + bot[3] * (1 - top[3]);
		if (a === 0) return [0, 0, 0, 0];
		return [0, 1, 2].map((i) => (top[i] * top[3] + bot[i] * bot[3] * (1 - top[3])) / a).concat(a);
	};
	const lum = (c: number[]): number => {
		const [r, g, b] = c.slice(0, 3).map((v) => {
			const s = v / 255;
			return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
		});
		return 0.2126 * r + 0.7152 * g + 0.0722 * b;
	};
	const ratioOf = (a: number[], b: number[]): number => {
		const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p);
		return (x + 0.05) / (y + 0.05);
	};
	const hex = (c: number[]): string =>
		'#' + c.slice(0, 3).map((v) => Math.round(v).toString(16).padStart(2, '0')).join('');
	const classes = (el: Element): string[] =>
		[...el.classList].filter((c) => !/^(s|svelte)-[\w-]{6,}$/.test(c));
	const generatedId = (id: string): boolean => /^(s-|svelte-)/.test(id);
	const ident = (el: Element): string | null => {
		const dc = el.getAttribute('data-component');
		if (dc) return `[data-component="${CSS.escape(dc)}"]`;
		const tid = el.getAttribute('data-testid');
		if (tid) return `[data-testid="${CSS.escape(tid)}"]`;
		const id = el.getAttribute('id');
		if (id && !generatedId(id)) return `#${CSS.escape(id)}`;
		return null;
	};
	const part = (el: Element): string =>
		el.tagName.toLowerCase() + (ident(el) ?? '') + classes(el).map((c) => '.' + c).join('');
	const label = (el: Element): string => {
		const own = el.tagName.toLowerCase() + classes(el).map((c) => '.' + c).join('');
		const chain: Element[] = [];
		for (let e = el.parentElement; e && e !== document.body; e = e.parentElement) {
			if (e.tagName === 'TR') return `tr > ${own}`;
			const id = ident(e);
			const cls = classes(e);
			if (!id && cls.length === 0) continue;
			chain.push(e);
			if (id) {
				const parts = chain.map((a) => part(a));
				return parts.join(' > ') + ` > ${own}`;
			}
		}
		if (chain.length > 0) return `${part(chain[0])} > ${own}`;
		return own;
	};
	/**
	 * The colors the browser paints for text `fg0` inside `el` and for the
	 * backdrop behind it. Each translucent fill is composited over the layers
	 * below it, and each element with opacity < 1 is a group: its own fill and
	 * everything inside it composite together first, then fade onto what lies
	 * behind it, as CSS paints them. `inner` is an extra innermost layer (a
	 * pseudo-element's own fill and opacity).
	 *
	 * `at` is a point under the text. Elements painted beneath it that are not
	 * its ancestors (a positioned progress fill behind a percentage label) are
	 * fills too: each joins the stack just above the ancestor it shares with
	 * the text, faded by its own opacity.
	 *
	 * Three backdrops have no CSS color to read: a replaced element (an
	 * `<img>`, `<canvas>` or `<svg>` under the text), a positioned
	 * pseudo-element fill (a row's `::before` gradient) and a background-image.
	 * They return `why` plus `via`, so `measure` hands the text to the Node-side
	 * pixel sampler (`sampleUnmeasuredBackdrops`) instead of guessing.
	 */
	const paint = (
		el: Element,
		fg0: number[],
		inner?: { bg: number[]; op: number },
		at?: { x: number; y: number }
	): { fg?: number[]; bg?: number[]; why?: string; via?: 'svg' | 'pixel' } => {
		const chain: Element[] = [];
		for (let e: Element | null = el; e; e = e.parentElement) chain.push(e);
		const under = new Map<Element, { bg: number[]; op: number; img: string; who: Element }[]>();
		const REPLACED = 'IMG, VIDEO, CANVAS, IFRAME, OBJECT, EMBED, svg';
		const pt = at ?? (() => {
			const r = el.getBoundingClientRect();
			return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
		})();
		/**
		 * A positioned ::before/::after on `e` that paints a fill under the
		 * point, as a layer this instrument cannot color. Its box is resolved
		 * from the computed offsets against `e`'s own box (the containing block
		 * a `position: relative` host gives it), so an edge bar that does not
		 * reach the text is not counted.
		 */
		/**
		 * The thickness of the edge band a pseudo's mask confines its fill to,
		 * or null when the mask is absent or not made only of strips tiled along
		 * one axis (a repeat-x layer is a horizontal strip as tall as its tile,
		 * a repeat-y layer a vertical one as wide as its tile).
		 */
		const maskEdgeBand = (ps: CSSStyleDeclaration): number | null => {
			const image = ps.getPropertyValue('mask-image') || ps.getPropertyValue('-webkit-mask-image');
			if (!image || image === 'none') return null;
			const repeats = (ps.getPropertyValue('mask-repeat') || ps.getPropertyValue('-webkit-mask-repeat'))
				.split(',')
				.map((v) => v.trim());
			const sizes = (ps.getPropertyValue('mask-size') || ps.getPropertyValue('-webkit-mask-size'))
				.split(',')
				.map((v) => v.trim().split(/\s+/).map((d) => (d.endsWith('px') ? parseFloat(d) : NaN)));
			const positions = (ps.getPropertyValue('mask-position') || ps.getPropertyValue('-webkit-mask-position'))
				.split(',')
				.map((v) => v.trim().split(/\s+/));
			// A strip counts as an edge band only when it is anchored to the
			// matching edge; one positioned mid-box could sit under the text.
			// Chromium computes `left 0 top 0` as `left 0px top 0px` (4 tokens);
			// the 2-token form is `0% 100%` or plain keywords.
			const edgeAxis = (tokens: string[], axis: 'x' | 'y'): boolean => {
				const keys = axis === 'x' ? ['left', 'right'] : ['top', 'bottom'];
				if (tokens.length === 4) {
					const [k, o] = axis === 'x' ? [tokens[0], tokens[1]] : [tokens[2], tokens[3]];
					return keys.includes(k ?? '') && o === '0px';
				}
				const v = axis === 'x' ? tokens[0] : tokens[1];
				return v === '0%' || v === '0px' || v === '100%' || keys.includes(v ?? '');
			};
			const layers = image.split(/,\s*(?=url\(|(?:repeating-)?(?:linear|radial|conic)-gradient\(|none)/).length;
			let band = 0;
			for (let i = 0; i < layers; i++) {
				const rep = repeats[i % repeats.length];
				const [w, h] = sizes[i % sizes.length] ?? [];
				const pos = positions[i % positions.length] ?? [];
				const anchored = rep === 'repeat-x' ? edgeAxis(pos, 'y') : rep === 'repeat-y' ? edgeAxis(pos, 'x') : false;
				if (!anchored) return null;
				const thick = rep === 'repeat-x' ? h : rep === 'repeat-y' ? w : NaN;
				if (!Number.isFinite(thick)) return null;
				band = Math.max(band, thick);
			}
			return band;
		};
		const pseudoFill = (e: Element) => {
			for (const pseudo of ['::before', '::after'] as const) {
				const ps = getComputedStyle(e, pseudo);
				if (ps.content === 'none' || ps.content === 'normal' || ps.display === 'none') continue;
				if (ps.position !== 'absolute' && ps.position !== 'fixed') continue;
				const r = e.getBoundingClientRect();
				const px = (v: string) => (v.endsWith('px') ? parseFloat(v) : NaN);
				const [l, t, w, h] = [px(ps.left), px(ps.top), px(ps.width), px(ps.height)];
				if (![l, t, w, h].some(Number.isNaN)) {
					const x0 = r.left + l;
					const y0 = r.top + t;
					if (pt.x < x0 || pt.x > x0 + w || pt.y < y0 || pt.y > y0 + h) continue;
					// An edge ring (PAIR-09's paired-row zigzag): every mask layer is
					// a strip tiled along one axis, so the fill paints only within
					// that strip's thickness of the box edges. A point deeper inside
					// sees no fill from this pseudo.
					const band = maskEdgeBand(ps);
					if (band !== null) {
						const inset = Math.min(pt.x - x0, x0 + w - pt.x, pt.y - y0, y0 + h - pt.y);
						if (inset > band) continue;
					}
				}
				const c = parse(ps.backgroundColor);
				if ((c && c[3] > 0) || ps.backgroundImage !== 'none') {
					return { bg: [0, 0, 0, 0], op: 1, img: `pseudo-element fill on ${label(e)}${pseudo}`, who: e };
				}
			}
			return null;
		};
		if (at) {
			const stack = document.elementsFromPoint(at.x, at.y);
			const self = stack.indexOf(el);
			for (const s of self < 0 ? [] : stack.slice(self + 1)) {
				if (chain.includes(s) || el.contains(s)) continue;
				let op = 1;
				let common: Element | null = s;
				for (; common && !chain.includes(common); common = common.parentElement) {
					op *= Number(getComputedStyle(common).opacity);
				}
				if (!common) continue;
				const cs = getComputedStyle(s);
				const c = parse(cs.backgroundColor);
				if (!c) return { why: `unparsed background ${cs.backgroundColor} on ${label(s)}` };
				const row = under.get(common) ?? [];
				if (s.matches(REPLACED)) row.push({ bg: [0, 0, 0, 0], op: 1, img: `replaced element ${label(s)}`, who: s });
				const pf = pseudoFill(s);
				if (pf) row.push(pf);
				if (c[3] > 0 || cs.backgroundImage !== 'none') {
					row.push({ bg: [c[0], c[1], c[2], c[3] * op], op: 1, img: cs.backgroundImage, who: s });
				}
				if (row.length) under.set(common, row);
			}
		}
		const layers: { bg: number[]; op: number; img: string; who: Element }[] = [];
		if (inner) layers.push({ bg: inner.bg, op: inner.op, img: 'none', who: el });
		for (const e of chain) {
			layers.push(...(under.get(e) ?? []));
			// The measured pseudo-element itself (pass 3) is not its own backdrop.
			const pf = inner && e === el ? null : pseudoFill(e);
			if (pf) layers.push(pf);
			const cs = getComputedStyle(e);
			const c = parse(cs.backgroundColor);
			if (!c) return { why: `unparsed background ${cs.backgroundColor}` };
			layers.push({ bg: c, op: Number(cs.opacity), img: cs.backgroundImage, who: e });
		}
		// Layers above the first opaque fill that no faded ancestor reaches
		// through are hidden, so they are neither measured nor required.
		let faded = -1;
		layers.forEach((l, i) => {
			if (l.op < 1) faded = i;
		});
		let top = layers.length - 1;
		for (let i = 0; i < layers.length; i++) {
			if (i > faded && layers[i].bg[3] >= 1) {
				top = i;
				break;
			}
		}
		for (let i = 0; i <= top; i++) {
			const img = layers[i].img;
			if (img.startsWith('replaced element ')) {
				return { why: img, via: layers[i].who.tagName.toLowerCase() === 'svg' ? 'svg' : 'pixel' };
			}
			if (img.startsWith('pseudo-element fill ')) return { why: img, via: 'pixel' };
			if (img && img !== 'none') return { why: `background-image on ${label(layers[i].who)}`, via: 'pixel' };
		}
		const frames: { outside: number[]; a: number }[] = [];
		let cur = [255, 255, 255, 1]; // the canvas
		for (let i = top; i >= 0; i--) {
			if (layers[i].op < 1) {
				frames.push({ outside: cur, a: layers[i].op });
				cur = [0, 0, 0, 0];
			}
			cur = over(layers[i].bg, cur);
		}
		let fg = over(fg0, cur);
		let bg = cur;
		for (let f = frames.pop(); f; f = frames.pop()) {
			fg = over([fg[0], fg[1], fg[2], fg[3] * f.a], f.outside);
			bg = over([bg[0], bg[1], bg[2], bg[3] * f.a], f.outside);
		}
		return { fg, bg };
	};

	/** True when a `filter` value changes colors, not only adds a drop shadow. */
	const repaints = (filter: string): boolean =>
		filter !== 'none' && filter.replace(/drop-shadow\((?:[^()]|\([^()]*\))*\)/g, '').trim() !== '';
	const CONTROL =
		'button, input, select, textarea, a[href], label, summary, [tabindex], [role="button"], [role="tab"], [role="menuitem"], [role="switch"], [role="checkbox"], [role="radio"], [role="option"], [role="slider"]';
	const findings: ContrastFinding[] = [];
	const unmeasured: UnmeasuredElement[] = [];
	const probes: ContrastFinding[] = [];
	let measured = 0;
	let backdropCount = 0;

	/** The opacity `el` paints at when it is painted, sized and active, else null. */
	const visibleOpacity = (el: Element): number | null => {
		if (el.closest('script, style, noscript, title, template')) return null;
		if (!el.checkVisibility({ opacityProperty: true, visibilityProperty: true })) return null;
		const rect = el.getBoundingClientRect();
		if (rect.width < 1 || rect.height < 1) return null;
		// WCAG 1.4.3 exempts inactive UI components. The app's inert classes
		// also style plain status rows ("Loading playlists..."), which are
		// text, not components, so a class exempts only when it sits on a control.
		if (el.closest('[disabled], [aria-disabled="true"], [inert]')) return null;
		const styledInert = el.closest('.rb-inert, .rust-inert');
		if (styledInert && styledInert.matches(CONTROL)) return null;
		let opacity = 1;
		for (let e: Element | null = el; e; e = e.parentElement) opacity *= Number(getComputedStyle(e).opacity);
		// Faint is not inactive: only the disabled/inert predicates above exempt
		// an element, so a 0.45-opacity button that still works is measured.
		return opacity;
	};

	/** Measure one painted string: `style` is the computed style it paints with. */
	const measure = (
		el: Element,
		suffix: string,
		text: string,
		style: CSSStyleDeclaration,
		inner: { bg: number[]; op: number } | undefined,
		ats: { x: number; y: number }[] | undefined,
		box: 'contents' | 'element'
	) => {
		const name = label(el) + suffix;
		// SVG text paints with `fill` over shapes, not over a CSS background;
		// it is out of this instrument's reach, not silently passed.
		if (el.closest('svg')) {
			unmeasured.push({ el: name, why: 'svg text (fill over shapes)' });
			return;
		}
		// A filter on the text or any ancestor repaints the computed colors
		// (grayscale, invert), so the pair this instrument reads is not the
		// painted one. A backdrop-filter does the same to the backdrop, unless
		// an opaque fill at or below it hides what it blurred.
		let hidden = false;
		for (let e: Element | null = el; e; e = e.parentElement) {
			const cs = getComputedStyle(e);
			const f =
				repaints(cs.filter)
					? 'filter'
					: !hidden && cs.backdropFilter && cs.backdropFilter !== 'none'
						? 'backdrop-filter'
						: null;
			if (f) {
				unmeasured.push({ el: name, why: `css ${f} on ${label(e)}` });
				return;
			}
			if ((parse(cs.backgroundColor)?.[3] ?? 0) >= 1) hidden = true;
		}
		const fg0 = parse(style.color);
		if (!fg0) {
			unmeasured.push({ el: name, why: `unparsed color ${style.color}` });
			return;
		}
		// The worst sample is the painted pair: a label straddling a progress
		// fill edge is as legible as its half over the fill.
		let worst: { fg: number[]; bg: number[]; ratio: number } | null = null;
		for (const at of ats ?? [centerOf(el.getBoundingClientRect())]) {
			const p = paint(el, fg0, inner, at);
			if (!p.fg || !p.bg) {
				const why = p.why ?? 'unmeasured';
				// A backdrop with pixels but no CSS color (gradient, svg, canvas,
				// img, pseudo fill): hand the text to the Node-side pixel sampler,
				// which needs an opaque color to compare against.
				// CSS opacity on the text or an ancestor fades the painted pair, which
				// the sampler's uncomposited color cannot see: that text stays unmeasured.
				let opacity = 1;
				for (let e: Element | null = el; e; e = e.parentElement) opacity *= Number(getComputedStyle(e).opacity);
				if (p.via && fg0[3] >= 1 && (!inner || inner.op >= 1) && opacity >= 1) {
					const id = el.getAttribute('data-contrast-backdrop') ?? String(backdropCount++);
					el.setAttribute('data-contrast-backdrop', id);
					unmeasured.push({
						el: name,
						why,
						backdrop: {
							id,
							via: p.via,
							text: text.slice(0, 24),
							fg: fg0,
							size: parseFloat(style.fontSize),
							bold: Number(style.fontWeight) >= 700,
							probe: !!el.closest('[data-contrast-probe]'),
							box
						}
					});
				} else {
					unmeasured.push({ el: name, why });
				}
				return;
			}
			const ratio = ratioOf(p.fg, p.bg);
			if (!worst || ratio < worst.ratio) worst = { fg: p.fg, bg: p.bg, ratio };
		}
		if (!worst) return;
		measured++;
		const { fg, bg, ratio } = worst;
		const size = parseFloat(style.fontSize);
		const bold = Number(style.fontWeight) >= 700;
		const need = size >= 24 || (size >= 18.66 && bold) ? 3 : 4.5;
		// Findings table prints this 2-decimal ratio; displayed 4.5 must not fail a 4.5 floor.
		const displayed = Math.round(ratio * 100) / 100;
		const row: ContrastFinding = {
			el: name,
			text: text.slice(0, 24),
			fg: hex(fg),
			bg: hex(bg),
			ratio: displayed,
			need,
			via: 'css',
		};
		if (el.closest('[data-contrast-probe]')) probes.push(row);
		if (displayed < need) findings.push(row);
	};

	const centerOf = (r: DOMRect) => ({ x: r.left + r.width / 2, y: r.top + r.height / 2 });

	const root = scope === undefined ? document.body : document.querySelector(scope);
	if (!root) return { measured, findings, unmeasured: [{ el: scope!, why: 'audit scope not found' }], probes };

	// Hit testing skips `pointer-events: none`, which a decorative fill under
	// a label usually carries; lift it for the audit so every fill is found.
	const hit = document.createElement('style');
	hit.textContent = '* { pointer-events: auto !important; }';
	document.head.append(hit);
	try {
		// 1. DOM text nodes, measured once per parent element.
		const seen = new Set<Element>();
		const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
		for (let n = walker.nextNode(); n; n = walker.nextNode()) {
			const text = (n.textContent ?? '').trim();
			if (!text) continue;
			const el = n.parentElement;
			if (!el || seen.has(el)) continue;
			seen.add(el);
			const opacity = visibleOpacity(el);
			if (opacity === null) continue;
			const range = document.createRange();
			range.selectNodeContents(n);
			const r = range.getBoundingClientRect();
			const ats = !(r.width && r.height)
				? undefined
				: [
						centerOf(r),
						...Array.from(range.getClientRects())
							.filter((l) => l.width > 2 && l.height > 0)
							.flatMap((l) => [l.left + 1, l.right - 1].map((x) => ({ x, y: l.top + l.height / 2 }))),
					];
			measure(el, '', text, getComputedStyle(el), undefined, ats, 'contents');
		}

		// 2. Text a form control paints without a DOM text node: its value, or its
		// placeholder (which paints with ::placeholder's color) when empty.
		const controls = root.querySelectorAll(
			'input:not([type="hidden"], [type="checkbox"], [type="radio"], [type="range"], [type="color"], [type="file"]), textarea, select'
		);
		for (const el of Array.from(controls)) {
			const opacity = visibleOpacity(el);
			if (opacity === null) continue;
			if (el instanceof HTMLSelectElement) {
				const text = el.selectedOptions[0]?.text.trim() ?? '';
				if (text) measure(el, '', text, getComputedStyle(el), undefined, undefined, 'element');
				continue;
			}
			const input = el as HTMLInputElement | HTMLTextAreaElement;
			if (input.value.trim()) measure(el, '', input.value.trim(), getComputedStyle(el), undefined, undefined, 'element');
			else if (input.placeholder.trim()) {
				measure(el, '::placeholder', input.placeholder.trim(), getComputedStyle(el, '::placeholder'), undefined, undefined, 'element');
			}
		}

		// 3. CSS-generated labels (`content: 'LOCK'`). Only quoted strings that
		// carry a letter or digit count; icon glyphs and empty decorations do not.
		for (const el of Array.from(root.querySelectorAll('*'))) {
			for (const pseudo of ['::before', '::after'] as const) {
				const ps = getComputedStyle(el, pseudo);
				if (ps.content === 'none' || ps.content === 'normal' || ps.display === 'none') continue;
				const text = [...ps.content.matchAll(/"((?:[^"\\]|\\.)*)"/g)].map((m) => m[1]).join('').trim();
				if (!/[\p{L}\p{N}]/u.test(text)) continue;
				if (ps.visibility !== 'visible' || Number(ps.opacity) <= 0) continue;
				const opacity = visibleOpacity(el);
				if (opacity === null) continue;
				measure(el, pseudo, text, ps, { bg: parse(ps.backgroundColor) ?? [0, 0, 0, 0], op: Number(ps.opacity) }, undefined, 'element');
			}
		}

		// 4. Canvas-painted text (waveform split-row labels): no CSS color to
		// read, so each canvas that painted text is reported unmeasured. A page
		// without the tracker fails loud rather than reporting no canvas text.
		const painted = (window as unknown as { __contrastCanvasText?: WeakSet<HTMLCanvasElement> })
			.__contrastCanvasText;
		if (!painted) unmeasured.push({ el: 'document', why: 'canvas text tracker not installed (trackCanvasText)' });
		else {
			for (const c of Array.from(root.querySelectorAll('canvas'))) {
				if (painted.has(c) && visibleOpacity(c) !== null) {
					unmeasured.push({ el: label(c), why: 'canvas-painted text (fillText)' });
				}
			}
		}
	} finally {
		hit.remove();
	}
	return { measured, findings, unmeasured, probes };
}


function _hex(rgb: readonly number[]): string {
	return `#${rgb
		.slice(0, 3)
		.map((c) => Math.round(c).toString(16).padStart(2, '0'))
		.join('')}`;
}

type _Box = { x: number; y: number; width: number; height: number };

const HIDE_STYLE_ID = 'contrast-backdrop-hide';

/**
 * Measures every finding `renderedContrastAudit` tagged `backdrop` (text over
 * a gradient or other background-image, a replaced `<svg>`, `<canvas>` or
 * `<img>`, or a positioned `::before`/`::after` fill) against the pixels the
 * browser rendered behind it (#5787, #8527, #10368; replaces the svg-only
 * `sampleSvgBackdrops`). All tagged text is hidden at once (`color:
 * transparent`, `text-shadow: none`), the viewport is screenshotted once at
 * device scale 1, and each text box is reduced to its worst ratio: every
 * pixel for svg, a 3 by 3 grid plus the four edge midpoints otherwise. Text
 * whose box is off-screen or zero-sized, or whose pixels are translucent,
 * stays unmeasured; so does text the audit never tagged (filters, canvas
 * fillText, svg-internal text, faded text).
 */
export async function sampleUnmeasuredBackdrops(
	page: Page,
	report: RenderedContrastReport
): Promise<RenderedContrastReport> {
	const tagged = report.unmeasured.filter((u) => u.backdrop);
	if (tagged.length === 0) return report;
	const findings = [...report.findings];
	const probes = [...report.probes];
	const unmeasured = report.unmeasured.filter((u) => !u.backdrop);
	let measured = report.measured;
	let png: Buffer;
	let boxes: Record<string, _Box>;
	try {
		boxes = await page.evaluate(
			({ styleId, want }) => {
				const style = document.createElement('style');
				style.id = styleId;
				style.textContent =
					'[data-contrast-backdrop], [data-contrast-backdrop] *, [data-contrast-backdrop]::before, [data-contrast-backdrop]::after ' +
					'{ color: transparent !important; -webkit-text-fill-color: transparent !important; text-shadow: none !important; caret-color: transparent !important; }';
				document.head.append(style);
				const out: Record<string, { x: number; y: number; width: number; height: number }> = {};
				for (const { id, box } of want) {
					const el = document.querySelector(`[data-contrast-backdrop="${id}"]`);
					if (!el) continue;
					let r: DOMRect;
					if (box === 'contents') {
						const range = document.createRange();
						range.selectNodeContents(el);
						r = range.getBoundingClientRect();
					} else if (box === 'element') {
						r = el.getBoundingClientRect();
					} else {
						throw new Error(`unknown backdrop box ${String(box)}`);
					}
					out[id] = { x: r.left, y: r.top, width: r.width, height: r.height };
				}
				return out;
			},
			{ styleId: HIDE_STYLE_ID, want: tagged.map((u) => ({ id: u.backdrop!.id, box: u.backdrop!.box })) }
		);
		png = await page.screenshot({ animations: 'disabled', caret: 'hide', scale: 'css' });
	} finally {
		await page.evaluate((styleId) => {
			document.getElementById(styleId)?.remove();
			for (const el of Array.from(document.querySelectorAll('[data-contrast-backdrop]'))) {
				el.removeAttribute('data-contrast-backdrop');
			}
		}, HIDE_STYLE_ID);
	}
	const img = PNG.sync.read(png);
	const image = { width: img.width, height: img.height, data: img.data as Uint8Array };
	for (const u of tagged) {
		const { id, via, text, fg, size, bold, probe } = u.backdrop!;
		const box = boxes[id];
		if (!box) {
			unmeasured.push({ el: u.el, why: `${u.why}; element gone before sampling` });
			continue;
		}
		const sample = worstBackdropRatio(image, box, fg, via);
		if ('why' in sample) {
			unmeasured.push({ el: u.el, why: `${u.why}; ${sample.why}` });
			continue;
		}
		measured++;
		const displayed = Math.round(sample.ratio * 100) / 100;
		const need = requiredRatio(size, bold);
		const row: ContrastFinding = { el: u.el, text, fg: _hex(fg), bg: _hex(sample.bg), ratio: displayed, need, via };
		if (probe) probes.push(row);
		if (displayed < need) findings.push(row);
	}
	return { measured, findings, unmeasured, probes };
}
