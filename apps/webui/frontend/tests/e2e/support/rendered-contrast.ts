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
 * self-contained: no imports, no closures over module scope.
 */

export interface ContrastFinding {
	/** `<parent's first class> > tag.own.classes`, stable across renders. */
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

export function renderedContrastAudit(): RenderedContrastReport {
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
	const label = (el: Element): string => {
		const own = el.tagName.toLowerCase() + classes(el).map((c) => '.' + c).join('');
		for (let e = el.parentElement; e && e !== document.body; e = e.parentElement) {
			// A table row carries only state classes (loaded, paired, first,
			// selected...), so its first class changes with library state and
			// would make the same cell a new label on every run. Name it `tr`.
			if (e.tagName === 'TR') return `tr > ${own}`;
			const first = classes(e)[0];
			if (first) return `${first} > ${own}`;
		}
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
		at?: { x: number; y: number }
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
		const p: { fg?: number[]; bg?: number[]; why?: string } = fg0
			? paint(el, fg0, inner, at ?? centerOf(el.getBoundingClientRect()))
			: { why: `unparsed color ${style.color}` };
		if (!p.fg || !p.bg) {
			unmeasured.push({ el: name, why: p.why ?? 'unmeasured' });
			return;
		}
		measured++;
		const { fg, bg } = p;
		const size = parseFloat(style.fontSize);
		const bold = Number(style.fontWeight) >= 700;
		const need = size >= 24 || (size >= 18.66 && bold) ? 3 : 4.5;
		const ratio = ratioOf(fg, bg);
		const row: ContrastFinding = {
			el: name,
			text: text.slice(0, 24),
			fg: hex(fg),
			bg: hex(bg),
			ratio: Math.round(ratio * 100) / 100,
			need,
		};
		if (el.closest('[data-contrast-probe]')) probes.push(row);
		if (ratio + 1e-9 < need) findings.push(row);
	};

	const centerOf = (r: DOMRect) => ({ x: r.left + r.width / 2, y: r.top + r.height / 2 });

	// Hit testing skips `pointer-events: none`, which a decorative fill under
	// a label usually carries; lift it for the audit so every fill is found.
	const hit = document.createElement('style');
	hit.textContent = '* { pointer-events: auto !important; }';
	document.head.append(hit);
	try {
		// 1. DOM text nodes, measured once per parent element.
		const seen = new Set<Element>();
		const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
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
			measure(el, '', text, getComputedStyle(el), undefined, r.width && r.height ? centerOf(r) : undefined);
		}

		// 2. Text a form control paints without a DOM text node: its value, or its
		// placeholder (which paints with ::placeholder's color) when empty.
		const controls = document.body.querySelectorAll(
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
		for (const el of Array.from(document.body.querySelectorAll('*'))) {
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
			for (const c of Array.from(document.body.querySelectorAll('canvas'))) {
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
