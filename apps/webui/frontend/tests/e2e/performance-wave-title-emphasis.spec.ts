import { expect, test } from '@playwright/test';
import type { APIRequestContext, Page } from '@playwright/test';

/**
 * Pin e585d3b67f4d (the maintainer, live on /performance): "Waveform (top) album art
 * looks good but the title below it is too loud (full white font) and too
 * wide. Empty and no track loaded for state there is too loud".
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

test('the waveform track title paints the secondary text token, not the primary one', async ({
	page,
	request
}) => {
	await _deckOneLoaded(page, request);
	// Both tokens are resolved through a real probe element rather than string
	// -compared against theme.css, so this asserts on the same rgb() values the
	// compositor uses and cannot drift from the theme's own units.
	const measured = await page.locator('.wave-track-name').first().evaluate((node) => {
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
		return { painted: getComputedStyle(node).color, dim, primary };
	});
	expect(measured.dim, 'the dim and primary tokens must differ, or this proves nothing').not.toBe(
		measured.primary
	);
	expect(
		measured.painted,
		'a title painted in the primary text token reads as loud as a control'
	).toBe(measured.dim);
});

test('the painted title clears the 4.5:1 AA floor against what is actually behind it', async ({
	page,
	request
}) => {
	await _deckOneLoaded(page, request);
	// The backdrop is found by walking ancestors for the first non-transparent
	// background, which is what a reader's eye does: the title's own
	// background is transparent, so the token it sits on is a fact about the
	// rendered tree rather than about the stylesheet.
	const measured = await page.locator('.wave-track-name').first().evaluate((node) => {
		const rgb = (value: string): [number, number, number] | null => {
			const parts = value.match(/[\d.]+/g);
			if (parts === null || parts.length < 3) return null;
			if (parts.length >= 4 && Number(parts[3]) === 0) return null;
			return [Number(parts[0]), Number(parts[1]), Number(parts[2])];
		};
		const painted = rgb(getComputedStyle(node).color);
		let backdrop: [number, number, number] | null = null;
		let element: Element | null = node;
		while (element !== null && backdrop === null) {
			backdrop = rgb(getComputedStyle(element).backgroundColor);
			element = element.parentElement;
		}
		return { painted, backdrop };
	});
	expect(measured.painted, 'the title must have a resolvable colour').not.toBeNull();
	expect(measured.backdrop, 'some ancestor must paint an opaque background').not.toBeNull();

	const luminance = (channels: [number, number, number]): number => {
		const [r, g, b] = channels
			.map((channel) => channel / 255)
			.map((channel) => (channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4));
		return 0.2126 * r + 0.7152 * g + 0.0722 * b;
	};
	const [lighter, darker] = [
		luminance(measured.painted!),
		luminance(measured.backdrop!)
	].sort((left, right) => right - left);
	const ratio = (lighter + 0.05) / (darker + 0.05);
	expect(
		ratio,
		`title ${JSON.stringify(measured.painted)} on ${JSON.stringify(measured.backdrop)} ` +
			`measures ${ratio.toFixed(2)}:1`
	).toBeGreaterThanOrEqual(AA_CONTRAST_FLOOR);
});

test('the title box shrinks to its own text instead of filling the gutter', async ({
	page,
	request
}) => {
	await _deckOneLoaded(page, request);
	// A CLONE with one short character, sized by the same scoped rules in the
	// same parent, is what separates `fit-content` from `100%`. The live node
	// cannot answer this on its own: every title in a generated fixture is
	// wider than the 92px gutter, and once `max-width: 100%` caps it the two
	// declarations produce the SAME used width. Cloning rather than editing the
	// live node keeps the app's own state untouched - this measures layout, it
	// does not stage a track with a convenient name.
	const measured = await page.locator('.wave-track-name').first().evaluate((node) => {
		const parent = node.parentElement;
		if (parent === null) throw new Error('the track name must have a container');
		const style = getComputedStyle(node);
		const clone = node.cloneNode(true) as HTMLElement;
		const label = clone.querySelector('span') ?? clone;
		label.textContent = 'x';
		clone.style.visibility = 'hidden';
		parent.appendChild(clone);
		const shortWidth = clone.getBoundingClientRect().width;
		clone.remove();
		return {
			width: node.getBoundingClientRect().width,
			containerWidth: parent.getBoundingClientRect().width,
			shortWidth,
			overflow: style.overflow,
			maxWidth: style.maxWidth
		};
	});
	expect(
		measured.containerWidth,
		'the gutter must have a real width for this comparison to mean anything'
	).toBeGreaterThan(10);
	expect(
		measured.shortWidth,
		`a one-character title still occupies ${measured.shortWidth.toFixed(1)}px of the ` +
			`${measured.containerWidth.toFixed(1)}px gutter - the box is filling its container ` +
			'rather than shrinking to its content'
	).toBeLessThan(measured.containerWidth / 2);
	// And the long, real title is still capped by the gutter rather than
	// spilling out of it, and clipped rather than overflowing - which is what
	// makes the hover scrub meaningful.
	expect(measured.width).toBeLessThanOrEqual(measured.containerWidth + 1);
	expect(measured.maxWidth, 'a long title must still be capped by the gutter').toBe('100%');
	expect(measured.overflow, 'clipping is what makes the hover scrub meaningful').toBe('hidden');
});

test('the no-track slate gives up the raised fill and the empty title is italic', async ({
	page
}) => {
	await _performanceReady(page);
	const slate = page.locator('.wave-art-slate.standalone').first();
	await expect(slate).toBeVisible({ timeout: 30_000 });
	const measured = await slate.evaluate((node) => {
		const raisedProbe = document.createElement('span');
		(node.closest('.perf-root') ?? document.body).appendChild(raisedProbe);
		raisedProbe.style.backgroundColor = 'var(--rb-panel-raised)';
		const raised = getComputedStyle(raisedProbe).backgroundColor;
		raisedProbe.remove();
		const name = document.querySelector('.wave-track-name.empty');
		return {
			slateBackground: getComputedStyle(node).backgroundColor,
			raised,
			emptyFontStyle: name === null ? null : getComputedStyle(name).fontStyle
		};
	});
	// Transparent, checked as "not the raised fill" AND as alpha 0, so a future
	// theme whose raised token happens to equal the panel cannot make this
	// assertion vacuous.
	expect(measured.slateBackground, 'a standalone slate must not paint the raised chrome fill').not.toBe(
		measured.raised
	);
	expect(measured.slateBackground.replace(/\s/g, '')).toMatch(/^rgba\(\d+,\d+,\d+,0\)$/);
	expect(measured.emptyFontStyle, 'the empty-state name needs its own quieter treatment').toBe(
		'italic'
	);
});
