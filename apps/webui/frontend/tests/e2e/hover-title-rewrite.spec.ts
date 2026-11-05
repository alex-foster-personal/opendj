/**
 * UX-FLOAT-03: a `title` the app rewrites while its control is hovered wins
 * over the string the single hover tooltip parked. These cases depend on
 * MutationObserver delivery order, so they run in a real browser against the
 * tooltip the app installs in +layout.svelte; nothing is stubbed. The hosts
 * are plain elements added to a live app-shell page, because the behavior
 * under test belongs to the document-level tooltip, not to any one control
 * (feedback-review-panel-lazy.spec.ts covers a real control).
 *
 * Regression lines:
 * - if leave puts the parked string back then a rewritten title goes stale
 * - if a rewrite just before leave (observer not yet flushed) is lost then
 *   the old text wins the race
 * - if a parked ancestor restores its old text then a row keeps stale help
 * - control: a title nobody rewrote restores to the same text
 */
import { expect, test, type Page } from '@playwright/test';

import { spendBootLanding } from './support/boot-landing';

// `/` is Library mode's Crate Desk with no app shell; /pairings mounts the
// root layout that installs the tooltip.
const APP_SHELL_ROUTE = '/pairings';
const HOST_ID = 'uxfloat03-host';
const ROW_ID = 'uxfloat03-row';
const CHIP_ID = 'uxfloat03-chip';
const AWAY = { x: 5, y: 795 };

test.beforeEach(async ({ page }) => {
	await spendBootLanding(page);
	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto(APP_SHELL_ROUTE);
	await expect(page.getByTestId('header-status-strip')).toBeVisible();
	await page.mouse.move(AWAY.x, AWAY.y);
});

/** A fixed, topmost host with `title`, optionally inside a titled row. */
async function addHost(page: Page, title: string, rowTitle?: string): Promise<void> {
	await page.evaluate(
		({ title, rowTitle, HOST_ID, ROW_ID, CHIP_ID }) => {
			const fixed = 'position:fixed;left:400px;top:300px;z-index:2147483647;';
			const button = document.createElement('button');
			button.textContent = 'host';
			button.setAttribute('title', title);
			if (rowTitle === undefined) {
				button.id = HOST_ID;
				button.style.cssText = `${fixed}width:120px;height:40px;`;
				document.body.appendChild(button);
				return;
			}
			const row = document.createElement('div');
			row.id = ROW_ID;
			row.setAttribute('title', rowTitle);
			row.style.cssText = `${fixed}width:300px;height:60px;background:#888;`;
			button.id = CHIP_ID;
			button.style.cssText = 'margin:10px;width:120px;height:40px;';
			row.appendChild(button);
			document.body.appendChild(row);
		},
		{ title, rowTitle, HOST_ID, ROW_ID, CHIP_ID }
	);
}

const tip = (page: Page) => page.locator('#single-hover-tip');

test('a title rewritten while hovered is shown on the open tip and restored on leave', async ({
	page
}) => {
	await addHost(page, 'Skin: Gothic. Click for Light');
	const host = page.locator(`#${HOST_ID}`);
	await host.hover();
	await expect(tip(page)).toHaveText('Skin: Gothic. Click for Light');
	await expect(host).not.toHaveAttribute('title', /.*/);

	await host.evaluate((el) => el.setAttribute('title', 'Skin: Light. Click for Default'));
	await expect(host).toHaveAttribute('data-tip', 'Skin: Light. Click for Default');
	await expect(host, 'the new title is parked too, so no native tooltip').not.toHaveAttribute(
		'title',
		/.*/
	);
	await expect(tip(page)).toHaveText('Skin: Light. Click for Default');

	await page.mouse.move(AWAY.x, AWAY.y);
	await expect(host).toHaveAttribute('title', 'Skin: Light. Click for Default');
	await expect(host).not.toHaveAttribute('data-tip-parked', /.*/);

	await host.hover();
	await expect(tip(page), 're-hover reads the new text').toHaveText(
		'Skin: Light. Click for Default'
	);
});

test('a title rewritten just before leave, before the observer flushes, is kept', async ({
	page
}) => {
	await addHost(page, 'Undo: add 1 track');
	const host = page.locator(`#${HOST_ID}`);
	await host.hover();
	await expect(host).toHaveAttribute('data-tip', 'Undo: add 1 track');

	// Rewrite and leave in one task, so the leave handler runs before the
	// MutationObserver microtask can adopt the new title.
	const raced = await host.evaluate((el) => {
		el.setAttribute('title', 'Nothing to undo');
		el.dispatchEvent(
			new PointerEvent('pointerout', { bubbles: true, relatedTarget: document.body })
		);
		return el.getAttribute('title');
	});
	expect(raced).toBe('Nothing to undo');
	await expect(host).toHaveAttribute('title', 'Nothing to undo');
	await expect(host).not.toHaveAttribute('data-tip-parked', /.*/);
	await expect(host).not.toHaveAttribute('data-tip', /.*/);
});

test('a parked ancestor whose title is rewritten restores the new text', async ({ page }) => {
	await addHost(page, 'Vocals chip', 'Stems: none');
	const row = page.locator(`#${ROW_ID}`);
	await page.locator(`#${CHIP_ID}`).hover();
	await expect(tip(page)).toHaveText('Vocals chip');
	await expect(row, 'the ancestor is parked while the chip tip shows').not.toHaveAttribute(
		'title',
		/.*/
	);

	await row.evaluate((el) => el.setAttribute('title', 'Stems: 4 on this machine'));
	await expect(row).toHaveAttribute('data-tip', 'Stems: 4 on this machine');
	await expect(row, 'still parked while the chip tip shows').not.toHaveAttribute('title', /.*/);

	await page.mouse.move(AWAY.x, AWAY.y);
	await expect(row).toHaveAttribute('title', 'Stems: 4 on this machine');
});

test('control: a title left alone while hovered restores to the same text', async ({ page }) => {
	await addHost(page, 'Loop length');
	const host = page.locator(`#${HOST_ID}`);
	await host.hover();
	await expect(tip(page)).toHaveText('Loop length');
	await expect(host).not.toHaveAttribute('title', /.*/);
	await page.mouse.move(AWAY.x, AWAY.y);
	await expect(host).toHaveAttribute('title', 'Loop length');
	await expect(host).not.toHaveAttribute('data-tip', /.*/);
});
