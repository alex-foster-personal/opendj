/**
 * SETUP-12014: the first-run wizard completes a folder import with the keyboard alone.
 *
 * Regression lines:
 * - if step changes do not move focus to the step heading -> broken
 * - if Enter on the path field does not run Check -> broken
 * - if Enter on the panel does not activate the primary action when enabled -> broken
 * - if this spec uses click(), check(), fill() or page.click -> broken
 */
import { expect, test, type Locator, type Page } from '@playwright/test';
import { join } from 'node:path';

import {
	type OnboardingEngine,
	startOnboardingEngine,
	writeMusicFolder
} from './support/onboarding-engine';
import {
	IMPORT_BUDGET_MS,
	landAndTimeWizard,
	readSetupStatus,
	setupDialog,
	waitForTerminalJob
} from './support/onboarding-wizard';

const TRACKS = 3;

let engine: OnboardingEngine;

test.beforeEach(async ({}, testInfo) => {
	engine = await startOnboardingEngine(testInfo.title.replace(/\W+/g, '-').slice(0, 40));
});

test.afterEach(async ({}, testInfo) => {
	if (testInfo.status !== testInfo.expectedStatus) {
		await testInfo.attach('engine.log', { body: engine.logTail(), contentType: 'text/plain' });
	}
	await engine.dispose();
});

function announcer(page: Page): Locator {
	return setupDialog(page).getByTestId('setup-announcer');
}

function focusIsInsideDialog(page: Page): Promise<boolean> {
	return page.evaluate(() => {
		const dialog = document.querySelector('[role="dialog"][aria-label="First-run setup"]');
		const active = document.activeElement;
		return dialog instanceof Node && active instanceof Node && dialog.contains(active);
	});
}

async function tabTo(
	page: Page,
	locator: Locator,
	options: { direction?: 'forward' | 'backward'; max?: number } = {}
): Promise<void> {
	const max = options.max ?? 25;
	const key = options.direction === 'backward' ? 'Shift+Tab' : 'Tab';
	for (let attempt = 0; attempt < max; attempt += 1) {
		const focused = await locator.evaluate((el) => {
			const active = document.activeElement;
			if (!(active instanceof Node)) {
				return false;
			}
			return el === active || el.contains(active);
		});
		if (focused) {
			return;
		}
		await page.keyboard.press(key);
		if (!(await focusIsInsideDialog(page))) {
			const activeHtml = await page.evaluate(() => {
				const active = document.activeElement;
				if (active instanceof HTMLElement) {
					return active.outerHTML.slice(0, 300);
				}
				return String(active);
			});
			throw new Error(
				`tabTo: focus left First-run setup after ${attempt + 1} ${key} presses; activeElement=${activeHtml}`
			);
		}
	}
	const activeHtml = await page.evaluate(() => {
		const active = document.activeElement;
		if (active instanceof HTMLElement) {
			return active.outerHTML.slice(0, 300);
		}
		return String(active);
	});
	throw new Error(
		`tabTo: target not focused after ${max} ${key} presses; activeElement=${activeHtml}`
	);
}

async function expectHeadingFocused(page: Page, name: string | RegExp): Promise<void> {
	const heading = setupDialog(page).getByRole('heading', { name, level: 3 });
	await expect(heading).toBeFocused();
}

test.describe('onboarding gauntlet: keyboard only', () => {
	test('the folder import completes with the keyboard alone', async ({ page }) => {
		const music = join(engine.musicRoot, 'Keyboard Crate');
		writeMusicFolder(music, TRACKS);
		const endpointStatuses: Record<string, number> = {};

		page.on('response', (response) => {
			const url = new URL(response.url());
			if (url.pathname === '/api/v1/setup/detect/folder') {
				endpointStatuses.detectFolder = response.status();
			}
			if (url.pathname === '/api/v1/setup/import/folder') {
				endpointStatuses.importFolder = response.status();
			}
			if (url.pathname === '/api/v1/setup/dismiss') {
				endpointStatuses.dismiss = response.status();
			}
		});

		await landAndTimeWizard(page, engine.origin);
		const dialog = setupDialog(page);
		await expect(dialog.getByRole('button', { name: 'Get started' })).toBeEnabled();
		await page.waitForFunction(() => {
			const heading = document.querySelector('[data-step-heading]');
			return heading instanceof HTMLElement && heading === document.activeElement;
		});
		await expect(announcer(page)).toHaveText('Welcome, step 1 of 5');

		await page.keyboard.press('Enter');
		await expect(announcer(page)).toHaveText('Find your music, step 2 of 5', { timeout: 30_000 });
		await expectHeadingFocused(page, 'Find your music');

		await page.keyboard.press('Enter');
		await expectHeadingFocused(page, 'Find your music');

		await tabTo(page, dialog.getByRole('button', { name: 'Back', exact: true }));
		await page.keyboard.press('Enter');
		await expectHeadingFocused(page, 'Welcome');
		await expect(announcer(page)).toHaveText('Welcome, step 1 of 5');
		await page.keyboard.press('Enter');
		await expect(announcer(page)).toHaveText('Find your music, step 2 of 5');

		// Plain Chromium skips the disabled native picker; the step's escape row
		// selects the folder branch without leaving the heading's forward tab ring.
		await tabTo(page, dialog.getByRole('button', { name: 'Choose a folder instead' }));
		await page.keyboard.press('Enter');
		await expect(
			setupDialog(page).getByRole('heading', { name: 'Point at a folder' })
		).toBeVisible();
		await expect(announcer(page)).toHaveText('Choose a folder first.');

		await page.evaluate(() => {
			(globalThis as unknown as { __announcerMutations?: number }).__announcerMutations = 0;
			const node = document.querySelector('[data-testid="setup-announcer"]');
			if (node === null) return;
			new MutationObserver(() => {
				const bag = globalThis as unknown as { __announcerMutations?: number };
				bag.__announcerMutations = (bag.__announcerMutations ?? 0) + 1;
			}).observe(node, { childList: true, characterData: true, subtree: true });
		});

		const pathField = dialog.getByRole('textbox', { name: 'Folder to import' });
		await page.keyboard.press('Tab');
		await expect(pathField).toBeFocused();
		await page.keyboard.type(music);
		const mutationsWhileTyping = await page.evaluate(
			() => (globalThis as unknown as { __announcerMutations?: number }).__announcerMutations ?? 0
		);
		expect(mutationsWhileTyping).toBe(0);

		const detectWait = page.waitForResponse((response) =>
			response.url().includes('/api/v1/setup/detect/folder')
		);
		await page.keyboard.press('Enter');
		const detectResponse = await detectWait;
		expect(detectResponse.ok()).toBe(true);
		await expect(dialog.getByText(`${TRACKS} audio files found.`)).toBeVisible();
		await expect(
			dialog.locator('p.counts strong').filter({ hasText: `${TRACKS} audio files` })
		).toBeVisible();

		const importWait = page.waitForResponse((response) =>
			response.url().includes('/api/v1/setup/import/folder')
		);
		await tabTo(page, dialog.getByRole('button', { name: /^Import (this folder|these folders)$/ }));
		await page.keyboard.press('Enter');
		const importResponse = await importWait;
		expect(importResponse.status()).toBe(202);
		const importBody = (await importResponse.json()) as { id: string };

		await expectHeadingFocused(page, 'Importing');
		await expect(announcer(page)).toHaveText('Importing, step 3 of 5');

		await waitForTerminalJob(engine.origin, importBody.id, IMPORT_BUDGET_MS);
		const progressContinue = dialog.getByRole('button', { name: 'Continue', exact: true });
		await expect(progressContinue).toBeEnabled({ timeout: IMPORT_BUDGET_MS });
		await page.keyboard.press('Enter');
		await expectHeadingFocused(page, 'Stems analysis');
		await expect(announcer(page)).toHaveText('Stems analysis, step 4 of 5');
		await page.keyboard.press('Enter');
		await expectHeadingFocused(page, 'Done');
		await expect(announcer(page)).toHaveText('Done, step 5 of 5');

		const dismissWait = page.waitForResponse((response) =>
			response.url().includes('/api/v1/setup/dismiss')
		);
		await tabTo(page, dialog.getByRole('button', { name: 'Start playing' }));
		await page.keyboard.press('Enter');
		const dismissResponse = await dismissWait;
		expect(dismissResponse.ok()).toBe(true);
		await expect(setupDialog(page)).toHaveCount(0);

		const after = await readSetupStatus(engine.origin);
		expect(after.library_empty).toBe(false);
		expect(after.tracks).toBeGreaterThanOrEqual(TRACKS);
		expect(endpointStatuses.detectFolder).toBe(200);
		expect(endpointStatuses.importFolder).toBe(202);
		expect((endpointStatuses.dismiss ?? 0) >= 200 && (endpointStatuses.dismiss ?? 0) < 300).toBe(
			true
		);
	});

	test('Escape still minimises to the chip', async ({ page }) => {
		await landAndTimeWizard(page, engine.origin);
		await page.keyboard.press('Escape');
		await expect(setupDialog(page)).toHaveCount(0);
		await expect(page.locator('.su-chip')).toBeVisible();
	});
});
