// requirement: PERFMODE-16
// [if] user switches to Gig with unset helper pref [then] opt-in dialog appears

import { expect, test } from '@playwright/test';

import { watchUiPrefsPuts, type UiPrefsPutWatch } from './support/ui-prefs-puts';

// Both tests end with the posture switched to Gig, and the root suite shares one
// engine and one ui-prefs.json across every spec. Left on disk, Gig posture arms
// the rescue ring writer (rescue-ring-writer.svelte.ts posts only in Gig) on
// every later /performance page, and a page promoted to leader once the previous
// test's 10 s mirror lease lapses auto-restores that ring
// (runPerformanceRescueAutoRestore). On a slow runner that replayed the previous
// test's decks into quickload-dblclick-guard.spec.ts mid-test, and its "Deck N
// BEAT SYNC needs a beat grid" toasts sat on the quick-load button it clicks.
// Put the server defaults back so no later spec inherits this spec's Gig, once
// the page's own posture and helper PUTs have landed (the page is still open
// here, and a Gig click's PUT landing after the reset would undo it).
let pageWrites: UiPrefsPutWatch | null = null;
test.beforeEach(({ page }) => {
	pageWrites = watchUiPrefsPuts(page);
});
test.afterEach(async ({ request }) => {
	await pageWrites?.settle();
	pageWrites = null;
	const put = await request.put('/api/v1/ui-prefs', {
		data: { gig_helper: 'unset', app_posture: 'prep' }
	});
	expect(put.ok(), `restore gig_helper/app_posture defaults: HTTP ${put.status()}`).toBe(true);
	const body = (await (await request.get('/api/v1/ui-prefs')).json()) as {
		app_posture?: unknown;
		gig_helper?: unknown;
	};
	expect([body.app_posture, body.gig_helper]).toEqual(['prep', 'unset']);
});

test('gig helper prompt offers enable on first Gig switch', async ({ page }) => {
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

	await page.getByRole('button', { name: 'Gig', exact: true }).click();
	const dialog = page.getByTestId('gig-helper-prompt');
	// 15s, not the 5s default: trunk red Tue 29 Sep 2026 (checkpoint 077b1358a81)
	// - the exact-SHA rerun passed clean and no gig-helper code changed between
	// the last green checkpoint and this one, so the 5s window was too tight
	// for a loaded runner's render, not a broken transition. Same allowance
	// pattern as brand-launch.spec.ts / artwork-reader-unavailable.spec.ts.
	await expect(dialog).toBeVisible({ timeout: 15_000 });
	await dialog.getByRole('button', { name: 'Enable', exact: true }).click();
	await expect(dialog).toBeHidden();
	await expect(page.getByTestId('gig-helper-on')).toBeVisible();
});

// [if] user picks No thanks [then] a later Gig switch does not reopen dialog, [else stop].
test('gig helper No thanks suppresses future prompts', async ({ page, request }) => {
	const put = await request.put('/api/v1/ui-prefs', {
		data: { gig_helper: 'unset', app_posture: 'prep' }
	});
	expect(put.ok()).toBeTruthy();

	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

	await page.getByRole('button', { name: 'Gig', exact: true }).click();
	const dialog = page.getByTestId('gig-helper-prompt');
	// See the matching comment in the first test above.
	await expect(dialog).toBeVisible({ timeout: 15_000 });
	await dialog.getByRole('button', { name: 'No thanks', exact: true }).click();
	await expect(dialog).toBeHidden();

	await page.getByRole('button', { name: 'Prep', exact: true }).click();
	await page.getByRole('button', { name: 'Gig', exact: true }).click();
	await expect(dialog).toBeHidden();
});
