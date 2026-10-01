import { expect, test, type Page } from '@playwright/test';

// Issue #3097: PerformanceAppNav (position: fixed, bottom-left, z-index 50)
// painted OVER BrowserPanel's `.bottom-bar` content - concretely, the "open
// dj" wordmark - at every tested viewport, because both are anchored to the
// same bottom-left rectangle. Direction matters here: the defect is nav
// occluding something UNDERNEATH it, not something occluding nav, so the
// check below samples each `.bottom-bar` descendant that geometrically
// intersects the nav's box and confirms it is the topmost painted element
// somewhere in that intersection - not merely that "something" is there
// (`elementFromPoint` returning non-null tells you nothing about WHICH
// element painted, and the issue's own filing never checked this).
//
// Bounding-box comparison alone is NOT enough either, and was the original
// filing's other blind spot: PlaylistTree's `.tree-scroll` clips with
// `overflow-y: auto`, so a scrolled-out row's `getBoundingClientRect()` can
// report a box that overlaps nav's box on paper while the ancestor scroll
// container has already clipped every pixel of it before nav's rectangle
// begins - a real investigation of this issue found exactly that for the
// "Playlists" tree row. Sampling real screen points and asking what is
// actually painted there cannot be fooled by an ancestor's clip the way a
// box-intersection check can.

const VIEWPORTS = [
	{ width: 1280, height: 720 },
	{ width: 1440, height: 900 },
	{ width: 1728, height: 1400 }
] as const;

const SAMPLE_FRACTIONS: Array<[number, number]> = [
	[0.5, 0.5],
	[0.1, 0.5],
	[0.9, 0.5],
	[0.5, 0.1],
	[0.5, 0.9]
];

const CONTROL_TESTID = 'zzz-overlap-control-probe';

type HiddenDescendant = { tag: string; testid: string; cls: string };

async function bottomBarDescendantsHiddenByNav(page: Page): Promise<HiddenDescendant[]> {
	const nav = page.locator('[data-testid="performance-app-nav"]');
	await expect(nav, 'performance-app-nav').toBeVisible();
	const navBox = await nav.boundingBox();
	expect(navBox, 'performance-app-nav boundingBox').not.toBeNull();

	return page.evaluate(
		({ navBox, fractions }) => {
			const bar = document.querySelector('.bottom-bar');
			if (bar === null) throw new Error('.bottom-bar not found');
			const navEl = document.querySelector('[data-testid="performance-app-nav"]');
			if (navEl === null) throw new Error('performance-app-nav not found in DOM');

			const intersectsNav = (r: DOMRect) =>
				!(
					r.right <= navBox.x ||
					r.left >= navBox.x + navBox.width ||
					r.bottom <= navBox.y ||
					r.top >= navBox.y + navBox.height
				);

			const hidden: HiddenDescendant[] = [];
			for (const el of bar.querySelectorAll('*')) {
				if (navEl.contains(el)) continue;
				const r = el.getBoundingClientRect();
				if (r.width === 0 || r.height === 0) continue;
				if (!intersectsNav(r)) continue;

				const paintedAsSelf = fractions.some(([fx, fy]) => {
					const x = r.left + r.width * fx;
					const y = r.top + r.height * fy;
					if (x < 0 || y < 0 || x > window.innerWidth || y > window.innerHeight) return false;
					const hit = document.elementFromPoint(x, y);
					return hit !== null && (hit === el || el.contains(hit));
				});
				if (!paintedAsSelf) {
					hidden.push({
						tag: el.tagName,
						testid: (el as HTMLElement).dataset?.testid ?? '',
						cls: typeof el.className === 'string' ? el.className : ''
					});
				}
			}
			return hidden;
		},
		{ navBox: navBox!, fractions: SAMPLE_FRACTIONS }
	);
}

for (const viewport of VIEWPORTS) {
	// REQ: PERF-UI-04
	test(`performance app-nav: bottom-bar content stays visible under the nav at ${viewport.width}x${viewport.height}`, async ({
		page
	}) => {
		await page.setViewportSize(viewport);
		await page.goto('/performance');
		await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
		await expect(page.locator('[data-testid="track-row"]').first()).toBeVisible({
			timeout: 30_000
		});

		// AC4: the nav's own links must stay visible AND hittable at all
		// times (this is why the fix must not use pointer-events: none).
		for (const testid of [
			'performance-nav-library',
			'performance-nav-reconcile',
			'performance-nav-dedup',
			'performance-nav-smartlists',
			'performance-nav-admin'
		]) {
			const link = page.locator(`[data-testid="${testid}"]`);
			await expect(link, testid).toBeVisible();
			const box = await link.boundingBox();
			expect(box, `${testid} boundingBox`).not.toBeNull();
			const hit = await page.evaluate(
				({ x, y, testid }) => {
					const el = document.elementFromPoint(x, y);
					return el !== null && (el as HTMLElement).closest(`[data-testid="${testid}"]`) !== null;
				},
				{ x: Math.round(box!.x + box!.width / 2), y: Math.round(box!.y + box!.height / 2), testid }
			);
			expect(hit, `${testid} is hittable at its own center`).toBe(true);
		}

		// Control FIRST, on the real page: inject a `.bottom-bar` child
		// positioned entirely inside the nav's box (nav's z-index 50 beats
		// an unstyled inserted node) and confirm the probe reports it
		// hidden. A probe that finds nothing has proven nothing until it
		// has been shown able to find something.
		const navBoxForControl = await page.locator('[data-testid="performance-app-nav"]').boundingBox();
		expect(navBoxForControl, 'performance-app-nav boundingBox (control)').not.toBeNull();
		await page.evaluate(
			({ box, testid }) => {
				const bar = document.querySelector('.bottom-bar');
				if (bar === null) throw new Error('.bottom-bar not found (control setup)');
				const probe = document.createElement('span');
				probe.textContent = 'x';
				probe.dataset.testid = testid;
				Object.assign(probe.style, {
					position: 'fixed',
					left: `${box.x + 2}px`,
					top: `${box.y + 2}px`,
					width: '10px',
					height: `${box.height - 4}px`
				});
				bar.appendChild(probe);
			},
			{ box: navBoxForControl!, testid: CONTROL_TESTID }
		);
		const withControl = await bottomBarDescendantsHiddenByNav(page);
		expect(
			withControl.some((hit) => hit.testid === CONTROL_TESTID),
			`control probe at (${navBoxForControl!.x + 2}, ${navBoxForControl!.y + 2}) was not reported hidden - ` +
				'the check cannot detect an element occluded by the nav, so its silence below proves nothing'
		).toBe(true);
		await page.evaluate((testid) => {
			document.querySelector(`[data-testid="${testid}"]`)?.remove();
		}, CONTROL_TESTID);

		// Real assertion: with the control removed, every remaining
		// .bottom-bar descendant that overlaps the nav's box is still
		// painted as itself somewhere in that overlap - nothing is fully
		// hidden underneath the fixed nav.
		const hidden = await bottomBarDescendantsHiddenByNav(page);
		expect(
			hidden,
			`.bottom-bar content hidden underneath performance-app-nav: ${JSON.stringify(hidden)}`
		).toEqual([]);
	});
}
