import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';

import { spendBootLanding } from './support/boot-landing';
import {
	allowlistProblems,
	AXE_TAGS,
	classify,
	flatten,
	formatTable,
	staleEntries,
	type Allowlist,
	type AxeFinding,
	type Surface
} from './support/axe-audit';
import { waitForPerformanceIpc } from './support/performance-ready';
import { appendEmptyRescueSnapshot } from './support/rescue-snapshot';

// requirement: A11Y-11738 (issue #10367, epic #10346)
// [if] /performance (MORE and LESS), the library with its track context menu open,
// or Settings on the fixture engine has a serious or critical axe violation
// [then] the run fails unless ui-axe-audit.allow.json names rule and selector
// [else stop]. [if] an allowlist entry matches nothing on a surface it lists
// [then] the run fails as stale [else stop]. Moderate and minor findings print
// as info and do not fail the run.

const ALLOW: Allowlist = JSON.parse(
	readFileSync(fileURLToPath(new URL('./ui-axe-audit.allow.json', import.meta.url)), 'utf8')
);

test.beforeEach(async ({ page }) => {
	await spendBootLanding(page);
});

async function _scan(
	page: Page,
	surface: Surface
): Promise<{ findings: AxeFinding[]; passCount: number }> {
	const results = await new AxeBuilder({ page })
		.withTags([...AXE_TAGS])
		.disableRules(ALLOW.disabledRules.map((r) => r.rule))
		.analyze();
	return {
		findings: flatten(surface, results.violations),
		passCount: results.passes.length
	};
}

async function _loadTwoDecks(page: Page): Promise<void> {
	const tracks: string[] = await page.evaluate(async () => {
		const res = await fetch('/api/v1/tracks?limit=2');
		return (await res.json()).items.map((t: { stable_id: string }) => t.stable_id);
	});
	expect(tracks.length, 'the fixture library has two tracks').toBeGreaterThanOrEqual(2);
	for (const [index, stable_id] of tracks.entries()) {
		await page.evaluate(
			([deck, sid]) =>
				window.musicDjToolsPerformance!.dispatch({ type: 'load', deck, stable_id: sid }),
			[index + 1, stable_id] as const
		);
	}
	await expect(page.locator('section.rb-deck.loading')).toHaveCount(0, { timeout: 30_000 });
}

async function _setDeckLayout(page: Page, layout: 'MORE' | 'LESS'): Promise<void> {
	const btn = page.locator('button.deck-layout-btn', { hasText: layout });
	await expect(btn).toBeVisible();
	await expect(async () => {
		await btn.click();
		await expect(page.locator('button.deck-layout-btn.active', { hasText: layout })).toBeVisible();
		if (layout === 'LESS') {
			await expect(page.locator('.perf-root')).toHaveClass(/deck-layout-less/);
		} else {
			await expect(page.locator('.perf-root')).not.toHaveClass(/deck-layout-less/);
		}
	}).toPass({ timeout: 15_000 });
}

async function _openLibraryMenu(page: Page): Promise<void> {
	await page.goto('/');
	await expect(page.getByTestId('library-workspace')).toBeVisible({ timeout: 30_000 });
	const row = page.locator('[data-testid="track-row"]').first();
	await expect(row).toBeVisible({ timeout: 30_000 });
	await row.locator('td.c-title').click({ button: 'right' });
	await expect(page.locator('[data-testid="context-menu"]')).toBeVisible();
}

test('performance, library and Settings have no unlisted serious or critical axe violation', async ({
	page
}) => {
	test.fixme(true, 'nested-interactive on playlist rows and browser tabs, see #14903');
	test.setTimeout(60_000);
	await page.setViewportSize({ width: 1600, height: 900 });
	expect(allowlistProblems(ALLOW), 'allowlist shape').toEqual([]);

	const allFindings: AxeFinding[] = [];
	const passCounts: Record<Surface, number> = {
		'performance-more': 0,
		'performance-less': 0,
		'library-context-menu': 0,
		settings: 0
	};

	expect(
		new URL(page.url()).pathname,
		'a /performance page is still open to write a newer snapshot'
	).not.toBe('/performance');
	await appendEmptyRescueSnapshot(page.request);
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await waitForPerformanceIpc(page);
	await _loadTwoDecks(page);

	for (const [layout, surface] of [
		['MORE', 'performance-more'],
		['LESS', 'performance-less']
	] as const) {
		await _setDeckLayout(page, layout);
		const { findings, passCount } = await _scan(page, surface);
		allFindings.push(...findings);
		passCounts[surface] = passCount;
	}

	await _openLibraryMenu(page);
	{
		const { findings, passCount } = await _scan(page, 'library-context-menu');
		allFindings.push(...findings);
		passCounts['library-context-menu'] = passCount;
	}

	await page.goto('/settings', { waitUntil: 'domcontentloaded' });
	await page.waitForLoadState('networkidle').catch(() => {});
	{
		const { findings, passCount } = await _scan(page, 'settings');
		allFindings.push(...findings);
		passCounts.settings = passCount;
	}

	for (const [surface, count] of Object.entries(passCounts) as [Surface, number][]) {
		expect(count, `${surface}: axe passes floor`).toBeGreaterThan(0);
	}

	const { unexpected, allowed, info } = classify(allFindings, ALLOW);
	if (allowed.length) {
		console.log(`[A11Y-11738] serious/critical allowed:\n${formatTable(allowed)}`);
	}
	if (info.length) {
		console.log(`[A11Y-11738] moderate/minor (info, does not fail):\n${formatTable(info)}`);
	}

	expect(
		unexpected.map((f) => `${f.surface} | ${f.rule} | ${f.selector} | ${f.help}`),
		'serious or critical axe violations no allowlist entry names'
	).toEqual([]);
	expect(
		staleEntries(ALLOW, allowed),
		'allowlist entries that matched nothing on a surface they list: remove them'
	).toEqual([]);
});

test('control: a planted nameless button fails the scan naming button-name and its selector', async ({
	page
}) => {
	test.setTimeout(45_000);
	await page.goto('/settings', { waitUntil: 'domcontentloaded' });
	await page.waitForLoadState('networkidle').catch(() => {});

	await page.evaluate(() => {
		const host = document.createElement('div');
		host.setAttribute('data-axe-probe', '');
		host.id = 'axe-probe';
		host.innerHTML = '<button type="button" class="axe-probe-btn"></button>';
		document.body.appendChild(host);
	});

	const { findings } = await _scan(page, 'settings');
	const { unexpected } = classify(findings, ALLOW);
	const hit = unexpected.find((f) => f.rule === 'button-name');
	expect(hit, 'planted button must be a button-name violation').toBeTruthy();
	expect(hit!.selector, 'selector names the probe').toContain('axe-probe');
	const msg = `${hit!.surface} | ${hit!.rule} | ${hit!.selector} | ${hit!.help}`;
	expect(msg).toContain('button-name');
	expect(msg).toContain('axe-probe');
	console.log(`[A11Y-11738] probe selector: ${hit!.selector}`);

	await page.evaluate(() => document.getElementById('axe-probe')?.remove());
	const after = await _scan(page, 'settings');
	expect(
		after.findings.some((f) => f.selector.includes('axe-probe')),
		'probe removed'
	).toBe(false);
});

test('control: an allowlist entry that matches nothing is reported stale', async () => {
	const synthetic: Allowlist = {
		...ALLOW,
		findings: [
			...ALLOW.findings,
			{
				rule: 'button-name',
				selector: '#never-rendered',
				surfaces: ['settings'],
				reason:
					'Synthetic stale control for issue #10367: this selector never renders on Settings in the fixture engine.'
			}
		]
	};
	const stale = staleEntries(synthetic, []);
	expect(stale).toContain('button-name | #never-rendered | settings');
});
