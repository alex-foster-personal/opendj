import { expect, test, type Locator, type Page } from '@playwright/test';

// PERF-UI-01 (issue #2303): agentic-testing AGT-02 timed out clicking SOURCE
// at the default Playwright 1280x720 viewport because the browser panel
// collapsed to ~23px and playlist-all-tracks sat outside the window.
// Playwright's isVisible() / click() can pass by scrolling the target into
// view; this spec asserts a POSITIVE viewport intersection with no scroll.

const VIEWPORTS = [
	{ width: 1280, height: 720 },
	{ width: 1440, height: 900 },
	{ width: 1728, height: 1400 }
] as const;

const OVERFLOW_TOLERANCE_PX = 2;

async function assertCenterInViewport(
	page: Page,
	locator: Locator,
	viewport: { width: number; height: number },
	label: string
): Promise<void> {
	await expect(locator, label).toBeVisible();
	const box = await locator.boundingBox();
	expect(box, `${label} boundingBox`).not.toBeNull();
	const cx = box!.x + box!.width / 2;
	const cy = box!.y + box!.height / 2;
	expect(cx, `${label} center x`).toBeGreaterThanOrEqual(0);
	expect(cx, `${label} center x`).toBeLessThanOrEqual(viewport.width);
	expect(cy, `${label} center y`).toBeGreaterThanOrEqual(0);
	expect(cy, `${label} center y`).toBeLessThanOrEqual(viewport.height);

	const elementHandle = await locator.elementHandle();
	expect(elementHandle, `${label} elementHandle`).not.toBeNull();

	// Issue #3097: `hit.ok` alone passes whenever ANY element sits at the
	// center - including one that covers the real target, as the fixed
	// bottom-left link strip did before PERF-UI-07 removed it. The check must
	// confirm the element AT the center
	// IS the target (or is contained by it, e.g. an inner span/svg).
	const hit = await page.evaluate(
		({ x, y, el }) => {
			const target = document.elementFromPoint(x, y);
			if (target === null) return { ok: false, tag: 'null', isTarget: false, testid: '' };
			const isTarget = target === el || (el as Element).contains(target);
			return {
				ok: true,
				tag: target.tagName,
				testid: (target as HTMLElement).dataset?.testid ?? '',
				isTarget
			};
		},
		{ x: Math.round(cx), y: Math.round(cy), el: elementHandle }
	);
	expect(hit.ok, `${label} elementFromPoint at center`).toBe(true);
	expect(
		hit.isTarget,
		`${label} elementFromPoint at center must be the target (or inside it), got ` +
			`<${hit.tag}> data-testid="${hit.testid}" instead`
	).toBe(true);
}

for (const viewport of VIEWPORTS) {
	test(`performance: playlist tree, track row and play/cue stay in-viewport at ${viewport.width}x${viewport.height}`, async ({
		page
	}) => {
		await page.setViewportSize(viewport);
		await page.goto('/performance');
		await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
		await expect(page.locator('[data-testid="track-row"]').first()).toBeVisible({
			timeout: 30_000
		});

		await expect(page.locator('.perf-root')).not.toHaveClass(/deck-layout-less/);
		for (const deck of [1, 2, 3, 4]) {
			const panel = page.locator(`.rb-deck[data-deck='${deck}']`).first();
			await expect(panel).toBeVisible();
			const box = await panel.boundingBox();
			expect(box, `deck ${deck} box`).not.toBeNull();
			expect(box!.height, `deck ${deck} height`).toBeGreaterThan(100);
		}

		await assertCenterInViewport(
			page,
			page.locator('[data-testid="playlist-all-tracks"]'),
			viewport,
			'playlist-all-tracks'
		);
		await assertCenterInViewport(
			page,
			page.locator('[data-testid="track-row"]').first(),
			viewport,
			'track-row'
		);
		await assertCenterInViewport(
			page,
			page.locator('[data-testid="play-deck-1"]'),
			viewport,
			'play-deck-1'
		);
		await assertCenterInViewport(
			page,
			page.locator('[data-testid="cue-deck-1"]'),
			viewport,
			'cue-deck-1'
		);
		await assertCenterInViewport(page, page.locator('.rb-mixer').first(), viewport, 'mixer');

		const overflow = await page.evaluate(() => {
			const root = document.querySelector('.perf-root');
			if (root === null) throw new Error('.perf-root not found');
			return { scrollHeight: root.scrollHeight, clientHeight: root.clientHeight };
		});
		expect(overflow.scrollHeight).toBeLessThanOrEqual(
			overflow.clientHeight + OVERFLOW_TOLERANCE_PX
		);

		const wave = page.locator('.rb-waverow').first();
		await expect(wave).toBeVisible();
		const waveBox = await wave.boundingBox();
		expect(waveBox).not.toBeNull();
		if (viewport.height <= 799) {
			expect(waveBox!.height).toBeLessThanOrEqual(25);
			await expect(page.locator('[data-testid="library-panels-collapse-bar"]')).toBeVisible();
		} else {
			expect(waveBox!.height).toBeGreaterThanOrEqual(40);
		}
	});
}
