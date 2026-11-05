import type { Page } from '@playwright/test';
import { PNG } from 'pngjs';

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
}

export interface UnmeasuredElement {
	el: string;
	why: string;
	/** Set when the backdrop is a replaced `<svg>`: `sampleSvgBackdrops` measures
	 * the text against its rendered pixels, found by `data-svg-backdrop`. */
	svgBackdrop?: { id: string; text: string; fg: number[]; size: number; bold: boolean };
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
	 * Two backdrops have no CSS color to read, so they make the text
	 * unmeasured rather than measured against the panel behind them: a
	 * replaced element (an `<img>` under a hover label) and a positioned
	 * pseudo-element fill (a row's `::before` gradient), whose box hit
	 * testing cannot see.
	 */
	const paint = (
		el: Element,
		fg0: number[],
		inner?: { bg: number[]; op: number },
		at?: { x: number; y: number }
	): { fg?: number[]; bg?: number[]; why?: string } => {
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
			if (img.startsWith('replaced element ') || img.startsWith('pseudo-element fill ')) return { why: img };
			if (img && img !== 'none') return { why: `background-image on ${label(layers[i].who)}` };
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
	let svgCount = 0;

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
		inner?: { bg: number[]; op: number },
		ats?: { x: number; y: number }[]
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
				// A replaced <svg> under the text (a drawn knob) has pixels, not a
				// CSS color: hand the text to the Node-side pixel sampler, which
				// needs an opaque color to compare against.
				// CSS opacity on the text or an ancestor fades the painted pair, which
				// the sampler's uncomposited color cannot see: that text stays unmeasured.
				let opacity = 1;
				for (let e: Element | null = el; e; e = e.parentElement) opacity *= Number(getComputedStyle(e).opacity);
				if (/^replaced element .*svg$/.test(why) && fg0[3] >= 1 && !inner && opacity >= 1) {
					const id = String(svgCount++);
					el.setAttribute('data-svg-backdrop', id);
					unmeasured.push({
						el: name,
						why,
						svgBackdrop: {
							id,
							text: text.slice(0, 24),
							fg: fg0,
							size: parseFloat(style.fontSize),
							bold: Number(style.fontWeight) >= 700
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
			measure(el, '', text, getComputedStyle(el), undefined, ats);
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
				if (text) measure(el, '', text, getComputedStyle(el));
				continue;
			}
			const input = el as HTMLInputElement | HTMLTextAreaElement;
			if (input.value.trim()) measure(el, '', input.value.trim(), getComputedStyle(el));
			else if (input.placeholder.trim()) {
				measure(el, '::placeholder', input.placeholder.trim(), getComputedStyle(el, '::placeholder'));
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
				measure(el, pseudo, text, ps, { bg: parse(ps.backgroundColor) ?? [0, 0, 0, 0], op: Number(ps.opacity) });
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

function _linear(c: number): number {
	const v = c / 255;
	return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
}

function _luminance(rgb: readonly number[]): number {
	return 0.2126 * _linear(rgb[0]) + 0.7152 * _linear(rgb[1]) + 0.0722 * _linear(rgb[2]);
}

function _ratio(a: readonly number[], b: readonly number[]): number {
	const la = _luminance(a);
	const lb = _luminance(b);
	return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

function _hex(rgb: readonly number[]): string {
	return `#${rgb
		.slice(0, 3)
		.map((c) => Math.round(c).toString(16).padStart(2, '0'))
		.join('')}`;
}

/**
 * Text over a replaced `<svg>` (a drawn knob) has no CSS backdrop color, so
 * `renderedContrastAudit` leaves it unmeasured and tags it `svgBackdrop`. This
 * measures it against what the browser actually rendered behind it: the text
 * is made transparent, the element is screenshotted, and the WORST pixel
 * against the text color is the result. That is conservative by design: a
 * pixel of the drawing that clashes with the text fails it, even if most of
 * the box is clear. Translucent pixels stay unmeasured, never guessed.
 */
export async function sampleSvgBackdrops(
	page: Page,
	report: RenderedContrastReport
): Promise<RenderedContrastReport> {
	const findings = [...report.findings];
	const unmeasured: UnmeasuredElement[] = [];
	let measured = report.measured;
	for (const u of report.unmeasured) {
		if (!u.svgBackdrop) {
			unmeasured.push(u);
			continue;
		}
		const { id, text, fg, size, bold } = u.svgBackdrop;
		const loc = page.locator(`[data-svg-backdrop="${id}"]`);
		// The inline color is saved with its priority and put back exactly, so a
		// second run sees the page it saw the first time.
		const saved = await loc.evaluate((el) => {
			const style = (el as HTMLElement).style;
			const prev = { value: style.getPropertyValue('color'), priority: style.getPropertyPriority('color') };
			style.setProperty('color', 'transparent', 'important');
			return prev;
		});
		let png: Buffer;
		try {
			png = await loc.screenshot({ animations: 'disabled', caret: 'hide' });
		} catch (err) {
			unmeasured.push({ el: u.el, why: `${u.why}; screenshot failed: ${String(err)}` });
			continue;
		} finally {
			await loc
				.evaluate(
					(el, prev) => {
						const style = (el as HTMLElement).style;
						style.removeProperty('color');
						if (prev.value) style.setProperty('color', prev.value, prev.priority);
						el.removeAttribute('data-svg-backdrop');
					},
					saved
				)
				.catch(() => undefined);
		}
		const img = PNG.sync.read(png);
		let worst: { ratio: number; bg: number[] } | null = null;
		let translucent = false;
		for (let i = 0; i < img.data.length; i += 4) {
			if (img.data[i + 3] < 255) {
				translucent = true;
				break;
			}
			const bg = [img.data[i], img.data[i + 1], img.data[i + 2]];
			const ratio = _ratio(fg, bg);
			if (!worst || ratio < worst.ratio) worst = { ratio, bg };
		}
		if (translucent || !worst) {
			unmeasured.push({ el: u.el, why: `${u.why}; ${translucent ? 'translucent pixels' : 'no pixels'}` });
			continue;
		}
		measured++;
		const displayed = Math.round(worst.ratio * 100) / 100;
		const need = size >= 24 || (size >= 18.66 && bold) ? 3 : 4.5;
		if (displayed < need) {
			findings.push({ el: u.el, text, fg: _hex(fg), bg: _hex(worst.bg), ratio: displayed, need });
		}
	}
	return { ...report, measured, findings, unmeasured };
}
