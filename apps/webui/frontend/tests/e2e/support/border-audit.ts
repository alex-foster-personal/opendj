/**
 * App-wide border audit (UIBORDER-01): find borders that double up or get
 * clipped, measured from Chromium's own layout rather than read from CSS.
 *
 * the maintainer, Tue 6 Oct 2026: "Check other borders aren't doubling up elsewhere in
 * app - recurring nit". Each instance had been fixed one at a time; this
 * finds the class.
 *
 * Two kinds are reported:
 *
 * - `double`: two elements each paint a visible border strip, and the strips
 *   sit edge to edge on the same axis (gap <= EDGE_EPSILON_PX), overlapping
 *   for at least MIN_RUN_PX along it. On screen that is one line twice as
 *   thick as its neighbors. It covers both shapes the nit took: siblings
 *   whose right and left borders touch, and a child whose bottom border sits
 *   directly on its parent's bottom border.
 * - `clipped`: a bordered element whose border strip on one side falls
 *   partly or wholly outside the padding box of an `overflow: hidden|clip`
 *   ancestor, while the element itself is still on screen inside it. The
 *   line is cut (a missing bottom or right edge).
 *
 * A hard-edged inset box-shadow (blur 0; `inset 0 0 0 1px` is a ring,
 * `inset 0 -1px 0` one edge) paints the same line as a border, so it counts
 * as one for `double` (Deck.svelte draws its edge this way).
 *
 * A border is "visible" when its style is not none/hidden, its width is > 0
 * and its color is not fully transparent. Tables with `border-collapse:
 * collapse` merge their cell borders, so cells inside one are skipped for
 * `double`. A border the same color as its own box's background is not a
 * line, so it is skipped too. Only elements that are rendered (non-zero box, visibility
 * visible, not inside a display:none or opacity:0 subtree) and on screen
 * count.
 *
 * `auditBorders` runs in the page via `page.evaluate`, so it must stay
 * self-contained: no imports, no closures over module scope.
 */

export type BorderSide = 'top' | 'right' | 'bottom' | 'left';

export type BorderFinding = {
	kind: 'double' | 'clipped';
	/** The element whose border is doubled or clipped. */
	selector: string;
	/** `double`: the other element of the pair. `clipped`: the clipping ancestor. */
	other: string;
	/** The side of `selector`'s border involved. */
	side: BorderSide;
	/** `double`: the side of `other`'s border that touches it. `clipped`: the same as `side`. */
	otherSide: BorderSide;
	/** `double`: the combined thickness of the two touching strips.
	 * `clipped`: how many px of the border's width are cut off. */
	px: number;
	/** How long (px) the doubled or clipped run is. */
	run: number;
	/** Viewport box [x0, y0, x1, y1] of the doubled run or the clipped element,
	 * for evidence crops. Not part of the allowlist key. */
	box: [number, number, number, number];
	/** Exact DOM identity of both elements (an nth-child chain from <body>),
	 * so repeated controls that share a selector stay distinct findings. Not
	 * part of the allowlist key; an entry's `count` caps how many it covers. */
	pair: string;
};

export type BorderAuditResult = {
	/** Elements inspected; a zero here means the audit measured nothing. */
	inspected: number;
	/** Elements with at least one visible border side. */
	bordered: number;
	findings: BorderFinding[];
};

export type BorderAllowEntry = {
	kind: BorderFinding['kind'];
	selector: string;
	other: string;
	side: BorderSide;
	otherSide: BorderSide;
	/** How many distinct element pairs this entry covers (2 for a seam both
	 * decks share). Matching more fails all of them: a selector that only
	 * names tag and classes must not silently cover a new, unrelated control
	 * that happens to share them. */
	count: number;
	/** Why this one is intentional. Required; an entry without one is refused. */
	reason: string;
};

/** A stable key for allowlist matching; ignores the measured px and run. */
export function findingKey(
	f: Pick<BorderFinding, 'kind' | 'selector' | 'other' | 'side' | 'otherSide'>
): string {
	return `${f.kind}|${f.side}/${f.otherSide}|${f.selector}|${f.other}`;
}

/** Splits findings into allowlisted and not, and reports allowlist entries
 * that matched nothing (stale entries hide future regressions). */
export function applyAllowlist<F extends BorderFinding, A extends BorderAllowEntry>(
	findings: readonly F[],
	allow: readonly A[]
): { unexpected: F[]; allowed: F[]; unusedAllow: A[] } {
	for (const entry of allow) {
		if (!entry.reason || entry.reason.trim().length < 10) {
			throw new Error(`border allowlist entry needs a reason: ${findingKey(entry)}`);
		}
	}
	for (const entry of allow) {
		if (!Number.isInteger(entry.count) || entry.count < 1) {
			throw new Error(`border allowlist entry needs a count of 1 or more: ${findingKey(entry)}`);
		}
	}
	const allowKeys = new Map(allow.map((entry) => [findingKey(entry), entry]));
	const pairsByKey = new Map<string, Set<string>>();
	for (const f of findings) {
		const key = findingKey(f);
		if (!pairsByKey.has(key)) pairsByKey.set(key, new Set());
		pairsByKey.get(key)!.add(f.pair);
	}
	const used = new Set<string>();
	const unexpected: F[] = [];
	const allowed: F[] = [];
	for (const f of findings) {
		const key = findingKey(f);
		const entry = allowKeys.get(key);
		if (entry && pairsByKey.get(key)!.size <= entry.count) {
			used.add(key);
			allowed.push(f);
		} else {
			unexpected.push(f);
		}
	}
	const unusedAllow = allow.filter((entry) => !used.has(findingKey(entry)));
	return { unexpected, allowed, unusedAllow };
}

export function formatFindings(findings: readonly BorderFinding[]): string {
	if (findings.length === 0) return '(none)';
	return findings
		.map(
			(f) =>
				`${f.kind.padEnd(7)} ${(f.side + '/' + f.otherSide).padEnd(13)} ${f.px}px run ${f.run}px  ${f.selector}  <->  ${f.other}`
		)
		.join('\n');
}

/**
 * Runs in the browser. `rootSelector` scopes which elements are audited
 * (ancestors outside it still clip). Returns findings de-duplicated per
 * key and element pair.
 */
export function auditBorders(rootSelector: string): BorderAuditResult {
	const EDGE_EPSILON_PX = 0.6;
	const MIN_RUN_PX = 3;
	const SIDES = ['top', 'right', 'bottom', 'left'] as const;
	type Side = (typeof SIDES)[number];

	const root = document.querySelector(rootSelector);
	if (!root) throw new Error(`border audit: root ${rootSelector} not found`);

	const vw = window.innerWidth;
	const vh = window.innerHeight;

	// Hashed svelte classes change on every edit, and state classes (tw-*
	// visibility flags, active, selected, inert) change with what is on
	// screen, so neither goes in a key.
	const STATE_CLASS =
		/^(svelte-|s-[A-Za-z0-9_-]{6,}$|tw-|is-|has-)|^(active|selected|open|disabled|expanded|focused|playing|loaded|empty|rb-inert)$/;
	const stableClasses = (el: Element): string[] =>
		[...el.classList].filter((c) => !STATE_CLASS.test(c)).slice(0, 2);
	const part = (el: Element): string => {
		const tag = el.tagName.toLowerCase();
		const testId = el.getAttribute('data-testid');
		if (testId) return `${tag}[data-testid="${testId}"]`;
		if (el.id && !/\d{3,}/.test(el.id)) return `${tag}#${el.id}`;
		const cls = stableClasses(el);
		return cls.length ? `${tag}.${cls.join('.')}` : tag;
	};
	const describe = (el: Element): string => {
		const parts: string[] = [];
		let cur: Element | null = el;
		while (cur && parts.length < 3 && cur !== document.body) {
			const p = part(cur);
			parts.unshift(p);
			// A class or id anchors the path well enough; bare tags need context.
			if (parts.length >= 2 && /[.#[]/.test(p)) break;
			cur = cur.parentElement;
		}
		return parts.join(' > ');
	};
	const uniquePath = (el: Element): string => {
		const parts: string[] = [];
		for (
			let cur: Element | null = el;
			cur && cur !== document.documentElement;
			cur = cur.parentElement
		) {
			const parent: Element | null = cur.parentElement;
			const index = parent ? [...parent.children].indexOf(cur) + 1 : 1;
			parts.unshift(`${cur.tagName.toLowerCase()}:nth-child(${index})`);
		}
		return parts.join(' > ');
	};

	const hiddenSubtree = (el: Element): boolean => {
		for (let cur: Element | null = el; cur; cur = cur.parentElement) {
			const cs = getComputedStyle(cur);
			if (cs.display === 'none' || Number(cs.opacity) === 0) return true;
		}
		return false;
	};
	const transparent = (color: string): boolean =>
		color === 'transparent' || /rgba\([^)]*,\s*0\)$/.test(color) || /\/\s*0\)$/.test(color);

	type Strip = {
		el: Element;
		side: Side;
		width: number;
		// Strip extent across the line (x for left/right, y for top/bottom).
		a0: number;
		a1: number;
		// Extent along the line.
		b0: number;
		b1: number;
		collapsed: boolean;
		color: string;
	};

	const all = [root, ...root.querySelectorAll('*')];
	let inspected = 0;
	const strips: Strip[] = [];
	const borderedEls = new Set<Element>();
	const shadowEls = new Set<Element>();
	const rects = new Map<Element, DOMRect>();

	for (const el of all) {
		if (el instanceof SVGElement && !(el instanceof SVGSVGElement)) continue;
		const r = el.getBoundingClientRect();
		if (r.width <= 0 || r.height <= 0) continue;
		if (r.right <= 0 || r.bottom <= 0 || r.left >= vw || r.top >= vh) continue;
		const cs = getComputedStyle(el);
		if (cs.visibility !== 'visible') continue;
		inspected++;
		const collapsed =
			!!el.closest('table') &&
			getComputedStyle(el.closest('table')!).borderCollapse === 'collapse' &&
			el.tagName !== 'TABLE';
		for (const side of SIDES) {
			const width = parseFloat(cs.getPropertyValue(`border-${side}-width`));
			const style = cs.getPropertyValue(`border-${side}-style`);
			const color = cs.getPropertyValue(`border-${side}-color`);
			if (!(width > 0) || style === 'none' || style === 'hidden' || transparent(color)) continue;
			let a0: number, a1: number, b0: number, b1: number;
			if (side === 'left') [a0, a1, b0, b1] = [r.left, r.left + width, r.top, r.bottom];
			else if (side === 'right') [a0, a1, b0, b1] = [r.right - width, r.right, r.top, r.bottom];
			else if (side === 'top') [a0, a1, b0, b1] = [r.top, r.top + width, r.left, r.right];
			else [a0, a1, b0, b1] = [r.bottom - width, r.bottom, r.left, r.right];
			strips.push({ el, side, width, a0, a1, b0, b1, collapsed, color });
			borderedEls.add(el);
			rects.set(el, r);
		}
		// `box-shadow: inset 0 0 0 1px` (a ring) and `inset 0 -1px 0` (one
		// edge) are border stand-ins: Deck.svelte draws its edge that way.
		// Only hard-edged ones (blur 0) are lines. The band on each side of
		// the padding box is spread plus or minus the offset.
		const shadow = cs.boxShadow;
		if (shadow && shadow !== 'none' && shadow.includes('inset')) {
			const pl = r.left + (parseFloat(cs.borderLeftWidth) || 0);
			const pr = r.right - (parseFloat(cs.borderRightWidth) || 0);
			const pt = r.top + (parseFloat(cs.borderTopWidth) || 0);
			const pb = r.bottom - (parseFloat(cs.borderBottomWidth) || 0);
			const re =
				/(rgba?\([^)]*\)|#[0-9a-fA-F]+|[a-z]+)\s+(-?[\d.]+)px\s+(-?[\d.]+)px\s+([\d.]+)px\s+(-?[\d.]+)px\s+inset/g;
			for (const m of shadow.matchAll(re)) {
				const [color, x, y, blur, spread] = [
					m[1],
					Number(m[2]),
					Number(m[3]),
					Number(m[4]),
					Number(m[5])
				];
				if (blur > 0 || transparent(color)) continue;
				const bands: Record<Side, number> = {
					top: spread + y,
					bottom: spread - y,
					left: spread + x,
					right: spread - x
				};
				for (const side of SIDES) {
					const width = bands[side];
					if (!(width > 0)) continue;
					let a0: number, a1: number, b0: number, b1: number;
					if (side === 'left') [a0, a1, b0, b1] = [pl, pl + width, pt, pb];
					else if (side === 'right') [a0, a1, b0, b1] = [pr - width, pr, pt, pb];
					else if (side === 'top') [a0, a1, b0, b1] = [pt, pt + width, pl, pr];
					else [a0, a1, b0, b1] = [pb - width, pb, pl, pr];
					strips.push({
						el,
						side,
						width,
						a0,
						a1,
						b0,
						b1,
						collapsed: false,
						color
					});
					shadowEls.add(el);
				}
			}
		}
	}
	// Drop strips in hidden subtrees (checked once per bordered element).
	const hidden = new Set([...borderedEls, ...shadowEls].filter(hiddenSubtree));
	const live = strips.filter((s) => !hidden.has(s.el));

	const findings = new Map<
		string,
		{
			kind: 'double' | 'clipped';
			selector: string;
			other: string;
			side: Side;
			otherSide: Side;
			px: number;
			run: number;
			box: [number, number, number, number];
			pair: string;
		}
	>();
	const add = (f: {
		kind: 'double' | 'clipped';
		selector: string;
		other: string;
		side: Side;
		otherSide: Side;
		px: number;
		run: number;
		box: [number, number, number, number];
		pair: string;
	}) => {
		const key = `${f.kind}|${f.side}|${f.otherSide}|${f.selector}|${f.other}|${f.pair}`;
		const prev = findings.get(key);
		if (!prev || prev.run < f.run) findings.set(key, f);
	};
	const round = (n: number) => Math.round(n * 10) / 10;

	// Parses rgb()/rgba() into [r, g, b, a]; anything else (a color() space
	// Chromium did not resolve) is reported as unknown rather than guessed.
	const rgba = (c: string): [number, number, number, number] | null => {
		const m = c.match(/^rgba?\(([\d.]+)[ ,]+([\d.]+)[ ,]+([\d.]+)(?:[ ,/]+([\d.]+))?\)$/);
		return m
			? [Number(m[1]), Number(m[2]), Number(m[3]), m[4] === undefined ? 1 : Number(m[4])]
			: null;
	};
	// The first non-transparent background at or above `el`.
	const effectiveBackground = (el: Element): [number, number, number, number] | null => {
		for (let cur: Element | null = el; cur; cur = cur.parentElement) {
			const bg = rgba(getComputedStyle(cur).backgroundColor);
			if (bg === null) return null;
			if (bg[3] > 0) return bg;
		}
		return [255, 255, 255, 1];
	};
	// A border the same color as the box it edges is not a line anyone sees
	// (WaveRow's 2px bottom border is its own background color). Unknown
	// colors count as visible, so an unparsed color can only add findings.
	const strokeShows = (s: Strip): boolean => {
		const line = rgba(s.color);
		const bg = effectiveBackground(s.el);
		if (line === null || bg === null) return true;
		const diff = Math.max(...[0, 1, 2].map((i) => Math.abs(line[i] - bg[i]))) * line[3];
		return diff >= 6;
	};
	// True when the strip's own element is the topmost hit at the strip's
	// center for along-line coordinate `along`. An ancestor on top means the
	// element ignores pointer events, which still paints, so that counts. A
	// descendant on top covers the border unless it paints nothing itself.
	const paintedAt = (s: Strip, along: number): boolean => {
		// Chromium hit-tests whole pixels, rounding a fractional point (100.5
		// lands on 101, the neighbor's pixel), so probe the first whole pixel
		// inside the strip.
		const first = Math.ceil(s.a0 - 0.01);
		const across = first < s.a1 ? first : (s.a0 + s.a1) / 2;
		const [x, y] =
			s.side === 'top' || s.side === 'bottom'
				? [Math.round(along), across]
				: [across, Math.round(along)];
		if (x < 0 || y < 0 || x >= vw || y >= vh) return false;
		const top = document.elementFromPoint(x, y);
		if (!top) return false;
		if (top === s.el || top.contains(s.el)) return true;
		if (!s.el.contains(top)) return false;
		if (/^(IMG|CANVAS|VIDEO|svg)$/i.test(top.tagName)) return false;
		const bg = rgba(getComputedStyle(top).backgroundColor);
		return bg !== null && bg[3] === 0;
	};

	// --- double: strips on the same axis, edge to edge.
	const horizontal = (s: Strip) => s.side === 'top' || s.side === 'bottom';
	const byAxis = [live.filter(horizontal), live.filter((s) => !horizontal(s))];
	for (const group of byAxis) {
		group.sort((p, q) => p.a0 - q.a0);
		for (let i = 0; i < group.length; i++) {
			const s = group[i];
			if (s.collapsed) continue;
			for (let j = i + 1; j < group.length; j++) {
				const t = group[j];
				if (t.a0 > s.a1 + EDGE_EPSILON_PX) break;
				if (t.el === s.el || t.collapsed) continue;
				// Edge to edge: t starts where s ends (t.a0 ~ s.a1). Overlapping
				// strips (one painted over the other) read as one line; skip.
				if (Math.abs(t.a0 - s.a1) > EDGE_EPSILON_PX) continue;
				const run = Math.min(s.b1, t.b1) - Math.max(s.b0, t.b0);
				if (run < MIN_RUN_PX) continue;
				// Both strips must actually be painted on top at the middle of
				// the shared run, or one is under an overlay (a popover, a card,
				// a closed <details>) and no double line shows.
				const mid = (Math.max(s.b0, t.b0) + Math.min(s.b1, t.b1)) / 2;
				if (!paintedAt(s, mid) || !paintedAt(t, mid)) continue;
				if (!strokeShows(s) || !strokeShows(t)) continue;
				// Opposite-facing or same-facing pairs both double. Report the
				// pair in document order so the key is stable.
				const [first, second] =
					s.el.compareDocumentPosition(t.el) & Node.DOCUMENT_POSITION_FOLLOWING ? [s, t] : [t, s];
				add({
					kind: 'double',
					selector: describe(first.el),
					other: describe(second.el),
					side: first.side,
					otherSide: second.side,
					px: round(s.width + t.width),
					run: round(run),
					box: horizontal(s)
						? [Math.max(s.b0, t.b0), s.a0, Math.min(s.b1, t.b1), t.a1]
						: [s.a0, Math.max(s.b0, t.b0), t.a1, Math.min(s.b1, t.b1)],
					pair: `${uniquePath(first.el)} | ${uniquePath(second.el)}`
				});
			}
		}
	}

	// --- clipped: border strip outside an overflow:hidden|clip ancestor's padding box.
	type ClipBox = { l: number; t: number; r: number; b: number };
	// Every ancestor that clips `el`: `hidden|clip` ones are what this audit
	// reports; `auto|scroll` ones clip scrolled content by design, so a border
	// a scroller also cuts is just scrolled partway out of view.
	const clippersOf = (el: Element) => {
		const hard: { el: Element; box: ClipBox }[] = [];
		const scroll: ClipBox[] = [];
		for (
			let cur = el.parentElement;
			cur && cur !== document.documentElement;
			cur = cur.parentElement
		) {
			const cs = getComputedStyle(cur);
			const mode = (o: string) =>
				o === 'hidden' || o === 'clip' ? 'hard' : o === 'auto' || o === 'scroll' ? 'scroll' : null;
			const mx = mode(cs.overflowX);
			const my = mode(cs.overflowY);
			if (!mx && !my) continue;
			const r = cur.getBoundingClientRect();
			const bl = parseFloat(cs.borderLeftWidth) || 0;
			const br = parseFloat(cs.borderRightWidth) || 0;
			const bt = parseFloat(cs.borderTopWidth) || 0;
			const bb = parseFloat(cs.borderBottomWidth) || 0;
			const boxFor = (kind: 'hard' | 'scroll'): ClipBox => ({
				l: mx === kind ? r.left + bl : -Infinity,
				r: mx === kind ? r.right - br : Infinity,
				t: my === kind ? r.top + bt : -Infinity,
				b: my === kind ? r.bottom - bb : Infinity
			});
			if (mx === 'hard' || my === 'hard') hard.push({ el: cur, box: boxFor('hard') });
			if (mx === 'scroll' || my === 'scroll') scroll.push(boxFor('scroll'));
		}
		return { hard, scroll };
	};
	const cutOn = (r: DOMRect, box: ClipBox, side: Side): number =>
		side === 'bottom'
			? r.bottom - box.b
			: side === 'top'
				? box.t - r.top
				: side === 'right'
					? r.right - box.r
					: box.l - r.left;
	for (const el of borderedEls) {
		if (hidden.has(el)) continue;
		const r = rects.get(el)!;
		const cs = getComputedStyle(el);
		// Fixed-position boxes escape every non-transformed clipping ancestor.
		if (cs.position === 'fixed') continue;
		const { hard, scroll } = clippersOf(el);
		for (const clip of hard) {
			const { box } = clip;
			// The element must be on screen inside the clipper, or it is just
			// scrolled or slid out of view (a carousel, a collapsed drawer).
			const visW = Math.min(r.right, box.r) - Math.max(r.left, box.l);
			const visH = Math.min(r.bottom, box.b) - Math.max(r.top, box.t);
			if (visW < Math.min(r.width, 4) || visH < Math.min(r.height, 4)) continue;
			const cx = (Math.max(r.left, box.l, 0) + Math.min(r.right, box.r, vw)) / 2;
			const cy = (Math.max(r.top, box.t, 0) + Math.min(r.bottom, box.b, vh)) / 2;
			const hit = document.elementFromPoint(cx, cy);
			if (!hit || !(el.contains(hit) || hit.contains(el))) continue;
			for (const side of SIDES) {
				const width = parseFloat(cs.getPropertyValue(`border-${side}-width`));
				const style = cs.getPropertyValue(`border-${side}-style`);
				const color = cs.getPropertyValue(`border-${side}-color`);
				if (!(width > 0) || style === 'none' || style === 'hidden' || transparent(color)) continue;
				const cut = cutOn(r, box, side);
				const run = side === 'top' || side === 'bottom' ? visW : visH;
				if (cut <= EDGE_EPSILON_PX / 2 || run < MIN_RUN_PX) continue;
				if (scroll.some((sbox) => cutOn(r, sbox, side) > 0)) continue;
				add({
					kind: 'clipped',
					selector: describe(el),
					other: describe(clip.el),
					side,
					otherSide: side,
					px: round(Math.min(cut, width)),
					run: round(run),
					box: [r.left, r.top, r.right, r.bottom],
					pair: `${uniquePath(el)} | ${uniquePath(clip.el)}`
				});
			}
		}
	}

	return {
		inspected,
		bordered: borderedEls.size,
		findings: [...findings.values()].sort((p, q) =>
			`${p.kind}${p.selector}${p.side}`.localeCompare(`${q.kind}${q.selector}${q.side}`)
		)
	};
}
