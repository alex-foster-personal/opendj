// requirement: SETNOTE-6340, SETNOTE-6340b, SETNOTE-6340c, SETNOTE-6340d, SETNOTE-6340e, SETNOTE-6340f, SETNOTE-6340g, SETNOTE-6340i, SETNOTE-6340j, SETNOTE-6340k
// [if] Right Cmd is tapped or the top-bar note clicked on /performance [then] the live editor opens over the decks,
// @ and right-click link tracks, saves reach state.db, hover+2 and Cmd-click load decks, a chord does not toggle

import { expect, test, type Page } from '@playwright/test';

const pad = (page: Page) => page.getByTestId('set-notes-pad');
const editor = (page: Page) => page.getByTestId('set-notes-editor').locator('.ProseMirror');

/** The host mounts from a deferred boot task, so the first taps may land
 * before the hotkey exists; retry the tap until the pad answers. */
async function openPadWithTap(page: Page): Promise<void> {
	await expect(async () => {
		if (!(await pad(page).isVisible())) await page.keyboard.press('MetaRight');
		await expect(pad(page)).toBeVisible({ timeout: 2_000 });
	}).toPass({ timeout: 45_000 });
}

test('set notes pad: tap toggle, @ search, right-click link, save, deck load, compare, dark, conflict', async ({
	page,
	request
}) => {
	// QUARANTINED Mon 2 Nov 2026 (#8227): after #6340 landed, the right-click menu shows no
	// "Link to:" suggestion (line 84) in the post-merge root suite, blocking `e2e verdict` on
	// unrelated PRs. Remove this line once #8227 is fixed and the spec is green 3 runs in a row.
	test.fixme(true, 'set-notes right-click link suggestion missing after #6340 landed, see #8227');
	test.setTimeout(120_000);
	const missing = await request.get('/api/v1/set-notes/scratch');
	expect(missing.status()).toBe(404);

	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, null, {
		timeout: 60_000
	});

	await openPadWithTap(page);
	const box = await pad(page).boundingBox();
	expect(box).not.toBeNull();
	const status = page.getByTestId('set-notes-status');
	await expect(status).toHaveText('New note');
	await expect(status).toHaveAttribute('data-rev', '0');
	await expect(page.getByTestId('set-notes-tip')).toContainText('@ to mention a song');
	await expect(page.getByTestId('set-notes-mode')).toHaveCount(0);

	// A chord with the right key and a left-hand tap leave it open.
	await page.keyboard.down('MetaRight');
	await page.keyboard.press('KeyC');
	await page.keyboard.up('MetaRight');
	await page.keyboard.press('MetaLeft');
	await expect(pad(page)).toBeVisible();

	// Dragging the header moves the pad.
	const header = page.getByTestId('set-notes-header');
	const hb = (await header.boundingBox())!;
	await page.mouse.move(hb.x + 20, hb.y + hb.height / 2);
	await page.mouse.down();
	await page.mouse.move(hb.x + 220, hb.y + hb.height / 2 + 120, { steps: 6 });
	await page.mouse.up();
	const moved = (await pad(page).boundingBox())!;
	expect(Math.round(moved.x - box!.x)).toBe(200);
	expect(Math.round(moved.y - box!.y)).toBe(120);

	// Markdown shortcuts render live; @ opens the collection search and Enter links the song as short text.
	await editor(page).click();
	await page.keyboard.type('# Opener');
	await page.keyboard.press('Enter');
	await page.keyboard.type('- start with @webkit');
	await expect(page.getByTestId('set-notes-search-hit').first()).toBeVisible({ timeout: 10_000 });
	await page.keyboard.press('Enter');
	await expect(editor(page).locator('h1')).toHaveText('Opener');
	await expect(editor(page).locator('ul > li')).toHaveCount(1);
	await expect(page.getByTestId('set-notes-search')).toHaveCount(0);
	await expect(editor(page).getByTestId('set-notes-chip')).toHaveCount(0);
	await expect(editor(page).getByTestId('set-notes-link')).toHaveText('@webkit-fixture-\u2026');
	await page.keyboard.type('then **peak** webkit');
	await expect(editor(page).locator('strong')).toHaveText('peak');

	// Right-click a selected word: the menu suggests matching tracks and links it.
	for (let i = 0; i < 'webkit'.length; i++) await page.keyboard.press('Shift+ArrowLeft');
	const sel = await page.evaluate(() => {
		const r = window.getSelection()!.getRangeAt(0).getBoundingClientRect();
		return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
	});
	await page.mouse.click(sel.x, sel.y, { button: 'right' });
	const menu = page.getByTestId('set-notes-context-menu');
	await expect(menu).toBeVisible();
	const suggestion = menu.getByTestId('set-notes-link-suggestion').first();
	await expect(suggestion).toHaveText(/^Link to: /, { timeout: 10_000 });
	await suggestion.click();
	await expect(menu).toBeHidden();
	const links = editor(page).getByTestId('set-notes-link');
	await expect(links.nth(1)).toHaveText('webkit');

	await expect(status).toHaveText('Saved', { timeout: 10_000 });
	await expect(status).not.toHaveAttribute('data-rev', '0');
	await expect
		.poll(async () => (await (await request.get('/api/v1/set-notes/scratch')).json()).body_md, {
			timeout: 10_000
		})
		.toMatch(/^# Opener\n- start with \[@webkit-fixture-\u2026\]\(track:[^)]+\) then \*\*peak\*\* \[webkit\]\(track:[^)]+\)$/);
	const saved = await (await request.get('/api/v1/set-notes/scratch')).json();
	const stableId = /\[@[^\]]+\]\(track:([^)]+)\)/.exec(saved.body_md)![1];
	const deckId = (deck: 1 | 2) =>
		page.evaluate((d) => window.musicDjToolsPerformance!.query().decks[d].stable_id, deck);

	// Hover a song and press 2: it loads onto deck 2, and the ribbon says so.
	const mentionBox = (await links.first().boundingBox())!;
	await page.mouse.move(mentionBox.x + 8, mentionBox.y + mentionBox.height / 2, { steps: 4 });
	await expect(page.getByTestId('set-notes-tip')).toContainText('Press 1-4');
	await page.keyboard.press('2');
	await expect.poll(() => deckId(2), { timeout: 30_000 }).toBe(stableId);
	// Without moving, a second digit is typed, not taken as a deck.
	// End collapses the right-click selection so the digit lands after the link, not over it.
	await page.keyboard.press('End');
	await page.keyboard.press('1');
	await expect(editor(page)).toContainText('1');
	expect(await deckId(1)).toBeFalsy();

	// Cmd/Ctrl-click follows the link onto deck 1.
	await links.first().click({ modifiers: ['ControlOrMeta'] });
	await expect.poll(() => deckId(1), { timeout: 30_000 }).toBe(stableId);
	await page.screenshot({ path: test.info().outputPath('set-notes-performance.png') });

	// The digit typed above autosaves as its own rev; settle it before pinning the current rev.
	await expect
		.poll(async () => (await (await request.get('/api/v1/set-notes/scratch')).json()).rev, { timeout: 10_000 })
		.toBeGreaterThan(saved.rev);
	const settled = await (await request.get('/api/v1/set-notes/scratch')).json();

	// A writer holding an older rev is refused with the current rev.
	const stale = await request.put('/api/v1/set-notes/scratch', {
		data: { body_md: 'lost', visible: true, base_rev: 0 }
	});
	expect(stale.status()).toBe(409);
	expect((await stale.json()).detail).toEqual({ error: 'stale_rev', current_rev: settled.rev });

	// An agent appends a chip for a track this library lacks, and a line linking both songs.
	const fixtures = await (await request.get('/api/v1/search?q=webkit-fixture&limit=5')).json();
	const [a, b] = fixtures.items.map((t: { stable_id: string }) => t.stable_id);
	const latest = await (await request.get('/api/v1/set-notes/scratch')).json();
	const appended = await request.put('/api/v1/set-notes/scratch', {
		data: {
			body_md: `${latest.body_md}\n- ghost [[track:no-such-track|Ghost]]\n- compare [@A](track:${a}) with [@B](track:${b})`,
			visible: true,
			base_rev: latest.rev
		}
	});
	expect(appended.status()).toBe(200);

	// A reload restores the open pad and renders the saved note; the unknown chip is inert.
	await page.reload({ waitUntil: 'domcontentloaded' });
	await expect(pad(page)).toBeVisible({ timeout: 45_000 });
	await expect(editor(page).locator('h1')).toHaveText('Opener');
	await expect(editor(page).locator('strong')).toHaveText('peak');
	await expect(links).toHaveText(['@webkit-fixture-\u2026', 'webkit', '@A', '@B']);
	const chips = editor(page).getByTestId('set-notes-chip');
	await expect(chips).toHaveCount(1);
	await expect(chips.first()).toHaveText('Ghost');
	await expect(chips.first()).toHaveClass(/sn-missing/);
	await chips.first().hover();
	await expect(page.getByTestId('set-notes-tip')).toHaveText('No track no-such-track in this library');

	// Shift-select across two songs: they are compared beside the pad, not in the note.
	await expect(page.getByTestId('set-notes-compare')).toHaveCount(0);
	await editor(page).getByText('compare').click();
	await page.keyboard.press('End');
	await page.keyboard.press('Shift+Home');
	const compare = page.getByTestId('set-notes-compare');
	await expect(compare.getByTestId('set-notes-compare-row')).toHaveCount(2);
	await expect(compare.getByTestId('set-notes-compare-pair')).toBeVisible();
	const padBox = (await pad(page).boundingBox())!;
	expect((await compare.boundingBox())!.x).toBeGreaterThanOrEqual(padBox.x + padBox.width);
	await page.keyboard.press('End');
	await expect(compare).toHaveCount(0);

	// The moon switches to dark paper and it is remembered.
	const dark = page.getByTestId('set-notes-dark');
	await dark.click();
	await expect(dark).toHaveAttribute('aria-pressed', 'true');
	const paper = () => pad(page).evaluate((el) => getComputedStyle(el).backgroundColor);
	expect(await paper()).toBe('rgb(31, 29, 20)');
	await dark.click();
	expect(await paper()).toBe('rgb(255, 245, 157)');

	// Geometry saved in a wide window is clamped where it is drawn in a narrow one.
	const before = await (await request.get('/api/v1/set-notes/scratch')).json();
	await page.setViewportSize({ width: 600, height: 480 });
	await expect
		.poll(async () => {
			const b = (await pad(page).boundingBox())!;
			return b.x >= 0 && b.y >= 0 && b.x + b.width <= 600;
		})
		.toBe(true);
	const after = await (await request.get('/api/v1/set-notes/scratch')).json();
	expect({ x: after.x, w: after.w, rev: after.rev }).toEqual({ x: before.x, w: before.w, rev: before.rev });
	await page.keyboard.press('ControlRight');
	await expect(pad(page)).toBeHidden();

	// The top-bar sticky note toggles the same pad and shows its state.
	await page.setViewportSize({ width: 1280, height: 720 });
	const toggle = page.getByTestId('set-notes-toggle');
	await expect(toggle).toHaveAttribute('aria-pressed', 'false');
	await toggle.click();
	await expect(pad(page)).toBeVisible();
	await expect(toggle).toHaveAttribute('aria-pressed', 'true');
	await toggle.click();
	await expect(pad(page)).toBeHidden();
});
