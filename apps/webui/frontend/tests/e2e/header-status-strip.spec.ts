import { expect, test } from '@playwright/test';

import { spendBootLanding } from './support/boot-landing';

// `/` is Library mode's full-window Crate Desk (LIBWS-01), which has no app
// shell; /pairings is a plain app-shell route that still mounts this header.
const APP_SHELL_ROUTE = '/pairings';

// Each test here opens `/` cold and reads the APP-SHELL header, which the
// PERFMODE-11 landing redirect replaces with the performance top bar mid-load;
// support/boot-landing.ts has the full account.
/** Since #5366 the track readout carries the playable count beside it. The
 *  separators carry their own spaces (`{' · '}`), and textContent adds the
 *  whitespace between flex items around them, so the gaps are `\s+`; the
 *  `.sep` checks below are what prove each separator keeps its spaces. */
const STRIP_TEXT = /\d+ tracks \(\d+ playable\)\s+·\s+\d+ playlists\s+·\s+lock: .+\s+·\s+bind: .+/;

test.beforeEach(async ({ page }) => {
	await spendBootLanding(page);
});

test('at 1280px the header status strip separates every readout with middle dots', async ({ page }) => {
	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto(APP_SHELL_ROUTE);

	const strip = page.getByTestId('header-status-strip');
	await expect(strip).toBeVisible();
	await expect(strip).toHaveText(STRIP_TEXT);
	await expect(strip).not.toHaveText(/playlistslock/i);
	// toHaveText reads textContent, which also holds the whitespace BETWEEN the
	// flex items, so it passed while every separator rendered as a bare "·".
	// Read the separators themselves (Wed 7 Oct 2026).
	const sepTexts = await strip.locator('.sep').evaluateAll((els) => els.map((el) => el.textContent));
	expect(sepTexts.length).toBeGreaterThanOrEqual(3);
	for (const text of sepTexts) expect(text).toBe(' · ');

	const tracks = strip.locator('.readout-numeric').nth(0);
	const playlists = strip.locator('.readout-numeric').nth(1);
	// The hover title is a sentence saying what the number counts (V1 polish,
	// PR #4923), and it leads with the same number the readout shows.
	await expect(tracks).toHaveAttribute('title', /^\d+ tracks: .+state\.db.* \d+ playable: /);
	await expect(playlists).toHaveAttribute('title', /^\d+ playlists: .+state\.db/);

	const tracksText = await tracks.innerText();
	const playlistsText = await playlists.innerText();
	const tracksTitle = await tracks.getAttribute('title');
	const playlistsTitle = await playlists.getAttribute('title');
	expect(tracksText.match(/^(\d+)/)?.[1]).toBe(tracksTitle?.match(/^(\d+)/)?.[1]);
	expect(playlistsText.match(/^(\d+)/)?.[1]).toBe(playlistsTitle?.match(/^(\d+)/)?.[1]);
	// The playable count in the readout is the one the hover title explains.
	const readoutPlayable = tracksText.match(/\((\d+) playable\)/)?.[1];
	expect(readoutPlayable, `no playable count in the readout: ${tracksText}`).toMatch(/^\d+$/);
	expect(readoutPlayable).toBe(tracksTitle?.match(/ (\d+) playable: /)?.[1]);
});

test('at narrow width the status strip never concatenates readouts', async ({ page }) => {
	await page.setViewportSize({ width: 720, height: 800 });
	await page.goto(APP_SHELL_ROUTE);

	const strip = page.getByTestId('header-status-strip');
	await expect(strip).toBeVisible();
	await expect(strip).toHaveText(STRIP_TEXT);

	const noRunOn = await strip.evaluate((el) => {
		const text = (el as HTMLElement).innerText.replace(/\s+/g, ' ');
		return !/playlistslock|freesync|n\/abind/i.test(text);
	});
	expect(noRunOn).toBe(true);

	const overflowHandled = await strip.evaluate((el) => {
		const contentWider = el.scrollWidth > el.clientWidth + 1;
		if (!contentWider) return true;
		const wrapped = el.clientHeight > 22;
		const ellipsized = Array.from(el.querySelectorAll('.readout')).some((node) => {
			const style = getComputedStyle(node);
			return (
				node.scrollWidth > node.clientWidth + 1 && style.textOverflow === 'ellipsis'
			);
		});
		return wrapped || ellipsized;
	});
	expect(overflowHandled).toBe(true);
});

test('only the CloudSync chip uses sync: in the app-shell header', async ({ page }) => {
	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto(APP_SHELL_ROUTE);

	const strip = page.getByTestId('header-status-strip');
	const chip = page.getByRole('button', { name: 'CloudSync status' });

	await expect(strip).toBeVisible();
	await expect(chip).toBeVisible();
	await expect(strip).not.toHaveText(/sync:/);
	await expect(strip).not.toHaveText(/sync: n\/a/);
	await expect(strip).not.toHaveText(/syncthing: n\/a/);
	await expect(chip.locator('.chip-label-full')).toHaveText(/sync:/);

	await chip.click();
	await expect(page.getByTestId('cloudsync-quick-actions-popover')).toBeVisible();
	await expect(page).not.toHaveURL(/\/cloudsync/);
});
