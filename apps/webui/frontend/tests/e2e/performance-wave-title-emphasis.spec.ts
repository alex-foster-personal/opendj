import { expect, test } from '@playwright/test';
import type { APIRequestContext, Page } from '@playwright/test';

/**
 * Pin e585d3b67f4d (the maintainer, live on /performance): "Waveform (top) album art
 * looks good but the title below it is too loud (full white font) and too
 * wide. Empty and no track loaded for state there is too loud".
 *
 * DECKUX-40 (the maintainer, Tue 6 Oct 2026, W29) redesigned the gutter and supersedes
 * the colour half of that pin: the title now sits right of full-height
 * artwork in the primary text token, over the live key and BPM, and fades at
 * its edge. The empty-state half went further: an empty deck shows only its
 * number ("empty state should I think be less messy - just show nothing"),
 * and missing artwork is empty space rather than a labelled slate.
 *
 * WHY THIS IS A COMPUTED-STYLE TEST AND NOT A SOURCE-TEXT ONE. The claims here
 * are all "what does the browser actually paint", and CSS has many ways for a
 * later rule to win that reading the stylesheet does not reveal. A blinded
 * reviewer of PR #1723 demonstrated five of them against a hand-written
 * cascade resolver, each reproduced by injecting real CSS into this very
 * component and watching every source-parsing guard stay green: a `@media`
 * wrapper, an `!important` at lower specificity, a `:where()` (which
 * contributes zero specificity), a `background-color` longhand overriding a
 * `background` shorthand, and a comma inside `:not()`. `getComputedStyle` is
 * subject to none of them, because it IS the cascade's answer.
 *
 * The palette-level AA arithmetic stays in
 * `tests/unit/wave-track-summary-emphasis.test.mjs`: it covers surfaces this
 * spec cannot reach (the light theme, and the deck 3/4 row fill) and needs no
 * browser to be true.
 */

const API_BASE = process.env.PERFORMANCE_E2E_API_BASE ?? 'http://127.0.0.1:8686';
const UI_BASE = process.env.PERFORMANCE_E2E_BASE_URL ?? 'http://127.0.0.1:5273';

/** WCAG 2.x AA for body text. `--rb-text-dim` was lightened to clear it. */
const AA_CONTRAST_FLOOR = 4.5;

async function _anyOnDiskTrack(request: APIRequestContext): Promise<string> {
	const response = await request.get(`${API_BASE}/api/v1/tracks?limit=50&available=true`);
	expect(response.ok(), 'track listing must succeed').toBeTruthy();
	const payload = (await response.json()) as {
		items: { stable_id: string; file_exists: boolean }[];
	};
	const track = payload.items.find((item) => item.file_exists);
	expect(track, 'this library must carry one on-disk track').toBeDefined();
	return track!.stable_id;
}

/**
 * `/performance` with the launch animation finished and the IPC installed.
 * The animation paints an opaque overlay over the whole app, and it is waited
 * OUT rather than skipped by seeding its localStorage key: pre-seeding is
 * fabricated application state, which this repo's acceptance evidence may not
 * rest on.
 */
async function _performanceReady(page: Page): Promise<void> {
	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto(`${UI_BASE}/performance`);
	await expect(page.getByLabel('Open DJ launch animation')).toBeHidden({ timeout: 20_000 });
	await page.waitForFunction(() => window.musicDjToolsPerformance !== undefined, null, {
		timeout: 60_000
	});
}

async function _deckOneLoaded(page: Page, request: APIRequestContext): Promise<void> {
	const stableId = await _anyOnDiskTrack(request);
	await _performanceReady(page);
	await page.evaluate(async (sid) => {
		await window.musicDjToolsPerformance!.dispatch({ type: 'load', deck: 1, stable_id: sid });
	}, stableId);
	await expect(page.locator('.wave-track-name').first()).toBeVisible({ timeout: 30_000 });
}

/**
 * Everything the title's legibility actually depends on, measured once in the
 * page and shared by the two tests below.
 *
 * Three things a blinded reviewer of this PR showed a naive reading misses:
 *
 * 1. `.wave-track-name` is a WRAPPER. The characters live in its inner
 *    `<span>`, which already carries rules of its own. `color` inherits, so
 *    the two agree today - but `.wave-track-name > span { color: var(--rb-text) }`
 *    repaints the visible text loud while leaving the wrapper untouched.
 *    So the leaf is what gets read.
 * 2. `getComputedStyle(el).color` is the SPECIFIED colour, not the pixel.
 *    `opacity` is a separate compositing step it cannot see, and a `color`
 *    alpha below 1 is a blend it does not perform. `opacity: 0.5` on the
 *    title is the single most likely next edit if a future pin says "still
 *    too loud", and it would halve the real contrast while leaving every
 *    naive assertion green. So the colour is COMPOSITED here: the alpha of
 *    the text colour times the accumulated `opacity` of every element
 *    between the text and the surface it sits on, blended over that surface.
 *    Opacity on the backdrop-painting element itself is deliberately NOT
 *    counted - it dims the text and its own background together, which
 *    leaves the ratio between them unchanged.
 * 3. `filter`, `mix-blend-mode` and a gradient `background-image` change the
 *    painted result in ways this arithmetic genuinely cannot model. They are
 *    collected as `blockers` and asserted empty, so the day one appears the
 *    test fails loudly instead of quietly reporting a colour nothing is
 *    painted in.
 *
 * The whole measurement is one `page.evaluate` because its body runs in the
 * browser and cannot see anything from this module's scope - one copy rather
 * than the same helpers redeclared per test.
 */
interface TitlePaint {
	/** The leaf's own `color`, uncomposited - what the token comparison needs. */
	painted: string;
	dim: string;
	primary: string;
	/** `color` alpha x accumulated ancestor opacity, in [0, 1]. */
	effectiveAlpha: number;
	/** The first fully opaque background behind the text. */
	backdrop: [number, number, number] | null;
	/** `painted` blended over `backdrop` at `effectiveAlpha`. */
	composited: [number, number, number] | null;
	blockers: string[];
}

async function _measureTitlePaint(page: Page): Promise<TitlePaint> {
	return await page
		.locator('.wave-track-name')
		.first()
		.evaluate((node): TitlePaint => {
			const leaf = node.querySelector(':scope > span') ?? node;
			const channels = (value: string): number[] | null => {
				// Only rgb()/rgba(). Chromium normally flattens color-mix() before
				// serializing, but `.rb-waverow.deck-focus` IS declared as
				// `color-mix(in srgb, rgba(255,255,255,0.08) 50%, var(--rb-bg))`
				// (WaveRow.svelte), and a digit scrape of an UNRESOLVED color-mix
				// string harvests a percentage and two colours' channels as
				// though they were one colour - a number about nothing, silently.
				// Returning null instead makes the caller report no backdrop,
				// which the assertions treat as a hard failure. Blinded review.
				if (!/^rgba?\(/.test(value)) return null;
				const parts = value.match(/[\d.]+/g);
				if (parts === null || parts.length < 3) return null;
				return parts.map(Number);
			};
			const alphaOf = (value: string): number => {
				const parsed = channels(value);
				if (parsed === null) return 0;
				return parsed.length >= 4 ? parsed[3] : 1;
			};

			// Both tokens through a real probe, so this compares the same rgb()
			// values the compositor uses rather than string-matching theme.css.
			const scope = node.closest('.perf-root') ?? document.body;
			const probe = document.createElement('span');
			scope.appendChild(probe);
			const resolve = (token: string): string => {
				probe.style.color = `var(${token})`;
				return getComputedStyle(probe).color;
			};
			const dim = resolve('--rb-text-dim');
			const primary = resolve('--rb-text');
			probe.remove();

			const painted = getComputedStyle(leaf).color;
			const paintedChannels = channels(painted);
			let alpha = alphaOf(painted);
			let backdrop: [number, number, number] | null = null;
			const blockers: string[] = [];
			let element: Element | null = leaf;
			while (element !== null) {
				const style = getComputedStyle(element);
				const name = element.className === '' ? element.tagName : String(element.className);
				if (style.filter !== 'none') blockers.push(`${name}: filter ${style.filter}`);
				if (style.mixBlendMode !== 'normal') {
					blockers.push(`${name}: mix-blend-mode ${style.mixBlendMode}`);
				}
				// `display: contents` returns a computed background and opacity from
				// `getComputedStyle` while generating no box and painting nothing.
				// Treating one as the backdrop would report a colour that is never
				// on screen, so it is skipped for paint purposes entirely - its
				// opacity has no effect either. Blinded review.
				const paints = style.display !== 'contents';
				if (backdrop === null && paints) {
					if (style.backgroundImage !== 'none') {
						blockers.push(`${name}: background-image ${style.backgroundImage}`);
					}
					const bg = channels(style.backgroundColor);
					const bgAlpha = alphaOf(style.backgroundColor);
					if (bg !== null && bgAlpha === 1) {
						backdrop = [bg[0], bg[1], bg[2]];
						// Opacity at and above the backdrop dims text and surface
						// alike, so it stops counting here. The BLOCKER scan does
						// not stop with it: a filter or mix-blend-mode on an
						// ancestor ABOVE this element (`.perf-root` carries the
						// app shell's own rules) re-composites the "opaque"
						// backdrop against whatever is behind it, so a contrast
						// ratio measured against this token would again be a
						// number about nothing. Blinded review, Thu 10 Sep 2026.
					} else if (bg === null) {
						// Unparseable and therefore undecidable: an unresolved
						// color-mix() may well paint. Falling through to the
						// opacity branch would drop a real layer and report a
						// ratio that is too FLATTERING, with nothing red to show
						// for it. Blocking is the honest answer. Blinded review.
						blockers.push(`${name}: unparseable background ${style.backgroundColor}`);
					} else if (bgAlpha > 0) {
						// Translucent: a genuine compositing layer this helper does
						// not blend. Same reasoning - a silently optimistic ratio is
						// the failure this file exists to prevent. A hovered
						// `.rb-waverow.secondary.deck-focus` is exactly this case.
						blockers.push(`${name}: translucent background ${style.backgroundColor}`);
					} else {
						alpha *= Number(style.opacity);
					}
				} else if (backdrop === null) {
					alpha *= Number(style.opacity);
				}
				element = element.parentElement;
			}

			const composited: [number, number, number] | null =
				paintedChannels === null || backdrop === null
					? null
					: ([0, 1, 2].map((i) => paintedChannels[i] * alpha + backdrop![i] * (1 - alpha)) as [
							number,
							number,
							number
						]);

			return { painted, dim, primary, effectiveAlpha: alpha, backdrop, composited, blockers };
		});
}

// REQ: DECKUX-40
test('the waveform track title paints the primary text token', async ({ page, request }) => {
	await _deckOneLoaded(page, request);
	const measured = await _measureTitlePaint(page);
	expect(measured.dim, 'the dim and primary tokens must differ, or this proves nothing').not.toBe(
		measured.primary
	);
	expect(measured.painted, 'DECKUX-40 paints the title in the primary text token').toBe(
		measured.primary
	);
});

// REQ: A11Y-03
test('the painted title clears the 4.5:1 AA floor against what is actually behind it', async ({
	page,
	request
}) => {
	await _deckOneLoaded(page, request);
	const measured = await _measureTitlePaint(page);
	expect(
		measured.blockers,
		'filter/mix-blend-mode/gradient change the painted result in ways this arithmetic ' +
			'cannot model - the ratio below would be a number about nothing'
	).toEqual([]);
	expect(measured.backdrop, 'some ancestor must paint an opaque background').not.toBeNull();
	expect(measured.composited, 'the title must have a resolvable painted colour').not.toBeNull();

	const luminance = (rgb: [number, number, number]): number => {
		const [r, g, b] = rgb
			.map((channel) => channel / 255)
			.map((channel) =>
				channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
			);
		return 0.2126 * r + 0.7152 * g + 0.0722 * b;
	};
	const [lighter, darker] = [luminance(measured.composited!), luminance(measured.backdrop!)].sort(
		(left, right) => right - left
	);
	const ratio = (lighter + 0.05) / (darker + 0.05);
	expect(
		ratio,
		`title ${JSON.stringify(measured.painted)} at effective alpha ` +
			`${measured.effectiveAlpha} composites to ${JSON.stringify(measured.composited)} ` +
			`on ${JSON.stringify(measured.backdrop)} and measures ${ratio.toFixed(2)}:1`
	).toBeGreaterThanOrEqual(AA_CONTRAST_FLOOR);
});

// REQ: DECKUX-40
test('a long title is clipped by its column and fades out at the edge', async ({
	page,
	request
}) => {
	await _deckOneLoaded(page, request);
	const measured = await page
		.locator('.wave-track-name')
		.first()
		.evaluate((node) => {
			const parent = node.parentElement;
			if (parent === null) throw new Error('the track name must have a container');
			const style = getComputedStyle(node);
			return {
				width: node.getBoundingClientRect().width,
				containerWidth: parent.getBoundingClientRect().width,
				overflow: style.overflow,
				mask: style.maskImage || style.webkitMaskImage
			};
		});
	expect(measured.containerWidth, 'the text column must have a real width').toBeGreaterThan(10);
	expect(measured.width).toBeLessThanOrEqual(measured.containerWidth + 1);
	expect(measured.overflow, 'clipping is what makes the hover scrub meaningful').toBe('hidden');
	expect(measured.mask, 'the clipped edge fades instead of cutting a letter').toContain(
		'linear-gradient'
	);
});

// REQ: DECKUX-40
test('an empty deck shows only its number', async ({ page }) => {
	await _performanceReady(page);
	const gutter = page.locator('.rb-waverow .gutter').first();
	await expect(gutter.locator('.deck-num')).toBeVisible({ timeout: 30_000 });
	await expect(gutter.locator('.deck-num')).toHaveText('1');
	await expect(gutter.locator('.wave-track-summary')).toHaveCount(0);
	await expect(gutter.locator('.wave-art')).toHaveCount(0);
	expect((await gutter.innerText()).trim(), 'nothing but the deck number is painted').toBe('1');
});

// REQ: DECKUX-40
test('missing artwork is empty space with a hover reason, not a labelled slate', async ({
	page,
	request
}) => {
	// Aborting the artwork request is the real failure mechanism, not a
	// fabricated flag: the <img> fires onerror and the component takes its
	// missing-artwork arm.
	await page.route('**/api/v1/tracks/*/artwork*', (route) => route.abort());
	const stableId = await _anyOnDiskTrack(request);
	await _performanceReady(page);
	await page.evaluate(async (sid) => {
		await window.musicDjToolsPerformance!.dispatch({ type: 'load', deck: 1, stable_id: sid });
	}, stableId);

	const art = page.locator('.wave-art[title^="No artwork found"]').first();
	await expect(art, 'a deck whose artwork request fails must keep an artwork slot').toBeAttached({
		timeout: 30_000
	});
	await expect(art).toHaveText('');
	const background = await art.evaluate((node) => getComputedStyle(node).backgroundColor);
	expect(background.replace(/\s/g, ''), 'missing artwork paints no fill').toMatch(
		/^rgba\(\d+,\d+,\d+,0\)$/
	);
});

// REQ: DECKUX-40
test('the waveform rows draw no divider between them', async ({ page }) => {
	await _performanceReady(page);
	const row = page.locator('.rb-waverow').first();
	await expect(row).toBeVisible({ timeout: 30_000 });
	const borderBottom = await row.evaluate((node) => getComputedStyle(node).borderBottomWidth);
	expect(borderBottom).toBe('0px');
	await expect(page.locator('.rb-waverow .bars'), 'the bars countdown is gone').toHaveCount(0);
});
