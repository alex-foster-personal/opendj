/**
 * requirement: ENRICH-01
 *
 * The enrich-on-open pill and card against the REAL engine on the root
 * suite's fixture library (two unanalyzed tracks, no stems): no stubbed
 * summary and no sessionStorage hide, unlike the specs that need it out of
 * their way.
 *
 * Every number is read from `GET /api/v1/enrich/summary` and every expected
 * word from the same pure policy the card renders (`$lib/enrich/enrich-card`),
 * so nothing here pins a count: the assertions stay true as the fixture or the
 * drain changes. The fixture is unanalyzed with pending stems, so the card
 * must show; the spec asserts that, so it cannot pass on the absent branch
 * alone.
 *
 * Issue #5748: the old fixed bottom-right card intercepted hovers and broke
 * lyrics-words e2e on main; measured here before the fix, it hid 48 controls
 * at 1280x720 as it opened (expanded, for the stems question) and 9 collapsed.
 * The card now opens as an in-flow top-bar pill, flagged when it needs the
 * user, and expands only on a click.
 *
 * Issue #8738: live on the root suite after f31668eebb (ENRICH-08 warning
 * lines match EnrichCardView). No in-file quarantine mark, not in
 * playwright.config.ts testIgnore, and not in (private path).
 *
 * Regression lines:
 * - if the pill shows when the summary says show=false, or is absent when it says show=true, then broken
 * - if the card expands without a click, or the pill's flag disagrees with needsAttention, then broken
 * - if the card's lane lines (capability warnings included) or stems line differ from the policy functions over the summary it fetched, then broken
 * - if Esc, a click outside or Less leaves the expanded card open, or a click inside closes it, then broken
 * - if the collapsed pill hides any control (elementFromPoint) at 1280x720, 1440x900 or 1920x1080, in Default or Gothic, then broken
 */
import { expect, test, type Page, type Response } from '@playwright/test';

import {
	type EnrichSummary,
	absentLines,
	analysisLines,
	collapsedLine,
	dudLines,
	lyricsLine,
	needsAttention,
	stemsText,
	warningLines
} from '../../src/lib/enrich/enrich-card';

const SUMMARY_PATH = '/api/v1/enrich/summary';
const CARD = '[data-testid="enrich-card"]';

type Covered = { control: string; hit: string };

type Summaries = { latest: EnrichSummary | null; count: number };

/** Observe (never fulfil) every summary the card itself receives. */
function watchCardSummaries(page: Page): Summaries {
	const seen: Summaries = { latest: null, count: 0 };
	page.on('response', (response: Response) => {
		if (new URL(response.url()).pathname !== SUMMARY_PATH || !response.ok()) return;
		void response
			.json()
			.then((body: EnrichSummary) => {
				seen.latest = body;
				seen.count += 1;
			})
			.catch(() => undefined);
	});
	return seen;
}

async function fetchSummary(page: Page): Promise<EnrichSummary> {
	return page.evaluate(async (path) => {
		const response = await fetch(path);
		if (!response.ok) throw new Error(`GET ${path} answered ${response.status}`);
		return (await response.json()) as EnrichSummary;
	}, SUMMARY_PATH);
}

/**
 * The lane lines the card draws for this summary, in order (EnrichCardView.svelte),
 * including the ENRICH-08 capability warnings (a lane that cannot run here, the
 * opt-in AI genre keys): exactly the summary's warnings, no more, no fewer.
 */
function expectedLaneLines(summary: EnrichSummary): string[] {
	const lyrics = lyricsLine(summary);
	return [
		...analysisLines(summary),
		...(lyrics ? [lyrics] : []),
		...dudLines(summary),
		...absentLines(summary),
		...warningLines(summary)
	].map((line) => line.text);
}

/**
 * Open /performance on a summary that has lane counts. The drain computes its
 * first coverage only after the boot grace (ended by the first library index),
 * so the card's boot fetch can see `lanes: {}`; wait for real lanes from the
 * page, then reload so the card's own fetch carries them.
 */
async function openWithLanes(page: Page): Promise<{ seen: Summaries; summary: EnrichSummary }> {
	await page.goto('/performance?muted=1');
	await expect(page.locator('[data-testid="track-table"]')).toBeVisible({ timeout: 60_000 });
	let summary = await fetchSummary(page);
	await expect
		.poll(
			async () => {
				summary = await fetchSummary(page);
				return Object.keys(summary.analysis?.lanes ?? {}).length;
			},
			{ timeout: 90_000, intervals: [1_000, 2_000], message: 'the drain never reported a lane on the fixture library' }
		)
		.toBeGreaterThan(0);
	const seen = watchCardSummaries(page);
	await page.reload();
	await expect(page.locator('[data-testid="track-table"]')).toBeVisible({ timeout: 60_000 });
	await expect.poll(() => seen.count, { timeout: 60_000, message: 'the card never fetched its summary' }).toBeGreaterThan(0);
	return { seen, summary };
}

test('the enrich pill and card show exactly what the summary and its policy say', async ({ page }) => {
	test.setTimeout(240_000);
	const { seen, summary } = await openWithLanes(page);
	const pill = page.getByTestId('enrich-pill');
	const card = page.locator(CARD);

	if (!summary.show) {
		// Valid either way: a library with nothing left gets no pill and no card.
		await expect(pill).toHaveCount(0);
		await expect(card).toHaveCount(0);
	}
	// The fixture is unanalyzed with pending stems: it must exercise the visible branch.
	expect(summary.show, 'fixture library no longer makes the card show; nothing below is exercised').toBe(true);
	await expect(pill).toBeVisible({ timeout: 30_000 });

	// Never auto-expands over the app (issue #5748); what needs the user flags the pill instead.
	await expect(card).toHaveCount(0);
	await expect(async () => {
		const shown = seen.latest as EnrichSummary;
		expect((await pill.getByTestId('enrich-collapsed').innerText()).trim()).toBe(collapsedLine(shown).text);
		await expect(pill.getByTestId('enrich-attention')).toHaveCount(needsAttention(shown, null, null) ? 1 : 0);
	}).toPass({ timeout: 15_000 });
	// The stems question needs the user on this fixture, so the flag is up.
	await expect(pill.getByTestId('enrich-attention')).toBeVisible();

	await pill.click();
	await expect(card).toBeVisible();
	await expect(async () => {
		const shown = seen.latest as EnrichSummary;
		expect(shown.show).toBe(true);
		const lanes = (await card.locator('p.enrich-line[data-lane]:not([data-lane="stems"])').allInnerTexts()).map((t) =>
			t.trim()
		);
		const want = expectedLaneLines(shown);
		expect(want.length, 'the summary has no lane line to compare').toBeGreaterThan(0);
		expect(lanes).toEqual(want);

		const stems = stemsText(shown.stems);
		const stemsLine = card.locator('p.enrich-line[data-lane="stems"]');
		if (stems === null) {
			await expect(stemsLine).toHaveCount(0);
		} else {
			expect((await stemsLine.innerText()).trim()).toBe(stems);
			// The numbers in it are the summary's, not recounted.
			if (shown.stems.separated !== undefined) expect(stems).toContain(`${shown.stems.separated.toLocaleString('en-US')} separated`);
			if (shown.stems.not_yet !== undefined) expect(stems).toContain(`${shown.stems.not_yet.toLocaleString('en-US')} not yet`);
		}
	}).toPass({ timeout: 45_000 });

	// Expanded by a click it may sit over content, so it must close every way.
	await page.keyboard.press('Escape');
	await expect(card).toHaveCount(0);
	await pill.click();
	await expect(card).toBeVisible();
	// Outside: the bottom bar's wordmark, plain text far from the card.
	await page.locator('.bottom-bar .wordmark').click();
	await expect(card).toHaveCount(0);
	await pill.click();
	await expect(card).toBeVisible();
	await card.getByRole('button', { name: 'Less' }).click();
	await expect(card).toHaveCount(0);
	// Control: a click INSIDE the card must not close it (an overshoot would close on any click).
	await pill.click();
	await card.locator('h3').click();
	await expect(card).toBeVisible();
	await expect(pill).toBeVisible();
});

const CONTROLS =
	'button, input, select, textarea, summary, a[href], [role="button"], [role="checkbox"], [role="slider"], [tabindex]:not([tabindex="-1"])';

type Overlap = { probeHitsPill: boolean; pillBox: { x: number; y: number; width: number; height: number }; nearby: number; covered: Covered[] };

/**
 * Every visible control whose box intersects the pill's box: elementFromPoint
 * at the center of that intersection must not land in the pill. `nearby`
 * counts the controls checked; `probeHitsPill` is the probe's own positive
 * control (elementFromPoint at the pill's center finds the pill).
 */
async function controlsUnderPill(page: Page): Promise<Overlap> {
	return page.evaluate((selector) => {
		const pill = document.querySelector('[data-testid="enrich-pill"]');
		if (!pill) throw new Error('missing enrich pill');
		const slot = pill.parentElement as HTMLElement;
		const box = slot.getBoundingClientRect();
		const describe = (el: Element): string => {
			const h = el as HTMLElement;
			const name = h.getAttribute('data-testid') ?? h.getAttribute('aria-label') ?? h.getAttribute('title') ?? h.textContent ?? '';
			return `${h.tagName.toLowerCase()}${h.className ? `.${String(h.className).split(' ')[0]}` : ''} "${name.trim().slice(0, 40)}"`;
		};
		const center = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
		const covered: Covered[] = [];
		let nearby = 0;
		for (const el of Array.from(document.querySelectorAll<HTMLElement>(selector))) {
			if (slot.contains(el) || el.contains(slot) || !el.checkVisibility()) continue;
			const r = el.getBoundingClientRect();
			const left = Math.max(r.left, box.left - 2);
			const right = Math.min(r.right, box.right + 2);
			const top = Math.max(r.top, box.top - 2);
			const bottom = Math.min(r.bottom, box.bottom + 2);
			if (right <= left || bottom <= top) continue;
			nearby += 1;
			const hit = document.elementFromPoint((left + right) / 2, (top + bottom) / 2);
			if (hit !== null && slot.contains(hit)) covered.push({ control: describe(el), hit: describe(hit) });
		}
		return {
			probeHitsPill: center !== null && slot.contains(center),
			pillBox: { x: Math.round(box.x), y: Math.round(box.y), width: Math.round(box.width), height: Math.round(box.height) },
			nearby,
			covered
		};
	}, CONTROLS);
}

async function setSkin(page: Page, skin: string): Promise<void> {
	const toggle = page.locator('[data-skin-current]');
	for (let i = 0; i < 3 && (await toggle.getAttribute('data-skin-current')) !== skin; i++) await toggle.click();
	await expect(toggle).toHaveAttribute('data-skin-current', skin);
}

for (const viewport of [
	{ width: 1280, height: 720 },
	{ width: 1440, height: 900 },
	{ width: 1920, height: 1080 }
]) {
	test(`the collapsed enrich pill covers no control at ${viewport.width}x${viewport.height}, Default and Gothic`, async ({ page }) => {
		test.setTimeout(240_000);
		await page.setViewportSize(viewport);
		await openWithLanes(page);
		const pill = page.getByTestId('enrich-pill');
		await expect(pill).toBeVisible({ timeout: 30_000 });
		await expect(page.locator(CARD)).toHaveCount(0);
		const skinToggle = page.locator('[data-skin-current]');
		const startSkin = (await skinToggle.getAttribute('data-skin-current')) ?? 'default';
		try {
			for (const skin of ['default', 'mono-dev']) {
				await setSkin(page, skin);
				const overlap = await controlsUnderPill(page);
				const label = `${skin} at ${viewport.width}x${viewport.height}, pill ${JSON.stringify(overlap.pillBox)}`;
				expect(overlap.probeHitsPill, `${label}: elementFromPoint at the pill's center does not find the pill`).toBe(true);
				expect(overlap.covered, `${label}: the pill hides controls: ${JSON.stringify(overlap.covered, null, 1)}`).toEqual([]);
				// The pill sits inside the top bar and the viewport, never past either edge.
				const fits = await page.evaluate(() => {
					const slot = document.querySelector('[data-testid="enrich-pill"]')?.parentElement?.getBoundingClientRect();
					const bar = document.querySelector('.rb-topbar')?.getBoundingClientRect();
					if (!slot || !bar) return 'missing pill or top bar';
					return slot.left >= bar.left && slot.right <= bar.right && slot.top >= bar.top && slot.bottom <= bar.bottom
						? 'inside'
						: `pill ${JSON.stringify(slot)} leaves the top bar ${JSON.stringify(bar)}`;
				});
				expect(fits, label).toBe('inside');
			}
		} finally {
			// ui_skin is a persisted pref on the shared backend: hand later specs what they had.
			await setSkin(page, startSkin);
		}
	});
}
