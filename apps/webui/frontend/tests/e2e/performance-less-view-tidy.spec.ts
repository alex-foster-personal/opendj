/**
 * requirement: LESSV-01, LESSV-02, LESSV-05
 *
 * the maintainer, Tue 6 Oct 2026, on the LESS (2-deck) view:
 * - "The deck heights should be the same as for MORE. Only the central mixer
 *   needs re-arranging to keep it all fitting vertically." (LESSV-01)
 * - hide the less important feature buttons in LESS (LESSV-02), and the
 *   playlist Set name / Set bar behind MORE (LESSV-05).
 *
 * Hidden is not removed: every hidden control stays mounted, MORE shows it
 * again, and the API routes and commands behind it keep working while it is
 * hidden, so an agent loses nothing.
 *
 * Regression lines:
 * - if deck 1 is a different height in LESS than in MORE at 800, 900 or 1080px tall then broken
 * - if any mixer control in LESS extends past the visible mixer panel then broken
 * - if a LESS-hidden control is unmounted rather than hidden, or MORE does not show it again, then broken
 * - if a route behind a LESS-hidden control stops answering while it is hidden then broken
 */
import { expect, test, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { FIXTURE_MANIFEST_PATH } from './playwright.performance.config';

type Manifest = { populated_playlist_id: string; tracks: { stable_id: string }[] };

function manifest(): Manifest {
	return JSON.parse(readFileSync(FIXTURE_MANIFEST_PATH, 'utf8')) as Manifest;
}

async function setLayout(page: Page, mode: 'MORE' | 'LESS'): Promise<void> {
	await page.locator('.deck-layout-toggle').getByRole('button', { name: mode, exact: true }).click();
	if (mode === 'LESS') {
		await expect(page.locator('.perf-root')).toHaveClass(/deck-layout-less/);
		await expect
			.poll(async () => (await page.locator(".rb-deck[data-deck='3']").boundingBox())?.height ?? -1)
			.toBeLessThanOrEqual(0.5);
	} else {
		await expect(page.locator('.perf-root')).not.toHaveClass(/deck-layout-less/);
		await expect
			.poll(async () => (await page.locator(".rb-deck[data-deck='3']").boundingBox())?.height ?? -1)
			.toBeGreaterThan(200);
	}
}

async function deckOneHeight(page: Page): Promise<number> {
	const box = await page.locator(".rb-deck[data-deck='1']").boundingBox();
	if (box === null) throw new Error('deck 1 has no box');
	return box.height;
}

/** The worst overshoot of any visible mixer control past the mixer's bottom edge. */
async function mixerOvershootPx(page: Page): Promise<{ px: number; who: string }> {
	return page.evaluate(() => {
		const mixer = document.querySelector('.rb-mixer');
		if (mixer === null) throw new Error('no .rb-mixer');
		const bottom = mixer.getBoundingClientRect().bottom;
		let worst = { px: -Infinity, who: '' };
		for (const el of mixer.querySelectorAll(
			'.strip-slot:not(.collapsed) *, .lower *, .deck-layout-toggle *'
		)) {
			const r = el.getBoundingClientRect();
			if (r.width === 0 || r.height === 0) continue;
			if (r.bottom - bottom > worst.px) worst = { px: r.bottom - bottom, who: el.className.toString() };
		}
		return worst;
	});
}

test('LESS keeps deck 1 the same height as MORE, and the whole mixer fits', async ({ page }) => {
	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto('/performance?muted=1');
	await page.waitForSelector('.rb-mixer');
	for (const viewport of [
		{ width: 1280, height: 800 },
		{ width: 1280, height: 900 },
		{ width: 1440, height: 900 },
		{ width: 1920, height: 1080 }
	]) {
		await page.setViewportSize(viewport);
		await setLayout(page, 'MORE');
		const more = await deckOneHeight(page);
		await setLayout(page, 'LESS');
		const less = await deckOneHeight(page);
		const label = `${viewport.width}x${viewport.height}`;
		expect(more, `${label}: MORE deck 1 must be a real deck`).toBeGreaterThanOrEqual(247);
		expect(Math.abs(less - more), `${label}: LESS deck 1 ${less}px vs MORE ${more}px`).toBeLessThanOrEqual(0.5);
		const overshoot = await mixerOvershootPx(page);
		expect(
			overshoot.px,
			`${label}: ${overshoot.who} ends ${overshoot.px}px past the LESS mixer panel`
		).toBeLessThanOrEqual(0);
	}
});

test('LESS hides the less important controls without unmounting them, and MORE shows them again', async ({
	page
}) => {
	// 1920 wide so no width tier (TopBar.svelte) evicts anything in MORE.
	await page.setViewportSize({ width: 1920, height: 1080 });
	await page.goto('/performance?muted=1');
	await page.waitForSelector('.rb-mixer');
	await page.getByTestId('playlist-row').filter({ hasText: 'E2E Fixture Set' }).click();
	await expect(page.getByTestId('playlist-set-tabs')).toBeAttached();

	const hidden = [
		['Stage', page.getByRole('button', { name: 'Open karaoke stage' })],
		['voice command', page.locator('input[aria-label="text command entry"]')],
		['LINK', page.locator('.link-btn')],
		['PAD', page.locator('.topbar-slot-pad')],
		['FX (unfinished)', page.getByRole('button', { name: 'FX panel' })],
		['Find & Replace', page.getByRole('button', { name: 'Find & Replace', exact: true })],
		['Bulk Edit', page.getByRole('button', { name: 'Bulk Edit', exact: true })],
		['Set bar', page.getByTestId('playlist-set-tabs')],
		['R (channel 1)', page.getByRole('button', { name: 'meter red anchor channel 1' })],
		['M (channel 1)', page.getByRole('button', { name: 'master ceiling channel 1' })]
	] as const;

	await setLayout(page, 'MORE');
	for (const [name, locator] of hidden) {
		await expect(locator, `${name} must be visible in MORE`).toBeVisible();
	}
	await setLayout(page, 'LESS');
	for (const [name, locator] of hidden) {
		await expect(locator, `${name} must be hidden in LESS`).toBeHidden();
		await expect(locator, `${name} must stay MOUNTED in LESS (hidden, not removed)`).toHaveCount(1);
	}
	// Kept on purpose: the real 2-deck toggle and MyTags.
	await expect(page.getByRole('button', { name: '2 deck view' })).toBeVisible();
	await expect(page.getByRole('button', { name: 'MyTags', exact: true })).toBeVisible();
	await setLayout(page, 'MORE');
	for (const [name, locator] of hidden) {
		await expect(locator, `${name} must come back in MORE`).toBeVisible();
	}
});

test('the routes behind LESS-hidden controls keep working while they are hidden', async ({ page }) => {
	const { populated_playlist_id: playlistId, tracks } = manifest();
	const stableId = tracks[0].stable_id;
	await page.setViewportSize({ width: 1440, height: 900 });
	await page.goto('/performance?muted=1');
	await page.waitForSelector('.rb-mixer');
	await setLayout(page, 'LESS');
	await expect(page.locator('input[aria-label="text command entry"]')).toBeHidden();

	// Voice command entry -> POST /api/v1/voice/probe.
	const probe = await page.request.post('/api/v1/voice/probe', { data: { text: 'load next track' } });
	expect(probe.status(), await probe.text()).toBe(200);

	// Find & Replace -> preview; Bulk Edit -> a no-op write with the preview's etag.
	const preview = await page.request.post('/api/v1/find-replace/preview', {
		data: { stable_ids: [stableId], field: 'comments', find: 'lessv-never-matches', replace: 'x' }
	});
	expect(preview.status(), await preview.text()).toBe(200);
	const row = ((await preview.json()) as { results: { etag: string; current_value: string | null }[] })
		.results[0];
	const bulk = await page.request.patch('/api/v1/bulk-edit', {
		data: { stable_ids: [stableId], expected_etags: { [stableId]: row.etag }, comments: row.current_value ?? '' }
	});
	expect(bulk.status(), await bulk.text()).toBe(200);

	// Set name / Set bar -> create then list a set on the playlist.
	const name = `lessv-${Date.now()}`;
	const created = await page.request.post(`/api/v1/playlists/${playlistId}/sets`, { data: { name } });
	expect(created.status(), await created.text()).toBeLessThan(300);
	const listed = await page.request.get(`/api/v1/playlists/${playlistId}/sets`);
	expect(listed.status()).toBe(200);
	expect(((await listed.json()) as { sets: { name: string }[] }).sets.map((s) => s.name)).toContain(name);
});
