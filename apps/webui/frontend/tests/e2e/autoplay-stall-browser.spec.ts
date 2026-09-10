import { expect, test, type Page } from '@playwright/test';

/**
 * PLAY-08's stall banner, in a real browser (issue #1640).
 *
 * WHAT THIS PROVES that the unit suite cannot. The SSR render test asserts
 * MARKUP; this asserts that the element is VISIBLE, that it appears without a
 * reload after the state changes, that its toggle works, and that neither the
 * banner nor its expanded list covers the controls underneath. All four are
 * layout and runtime facts, and all four were named as gaps by review
 * (r3973806306, r3974734057).
 *
 * THE LAST TEST DRIVES A REAL EXHAUSTION end to end (Codex r3974819402): a
 * disposable single-track playlist through the real HTTP API, opened through
 * the real browser panel, its one track loaded, mastered, played and seeked
 * into its own trigger window through the real performance IPC, and then the
 * production controller's own poll reaches its terminal branch and the banner
 * appears. Nothing about the stall is injected there. The earlier tests raise
 * the stall through the store instead, because they need shapes a real
 * exhaustion cannot conveniently produce - fifteen blocked tracks for the cap
 * and the list geometry - and because a layout assertion should not also be
 * paying for a real audio decode.
 *
 * Runs under playwright.autoplay-stall-gate.config.ts (real backend, real
 * throwaway fixture library), not the default config.
 */

const BANNER = '[data-testid="autoplay-stall-banner"]';
const TRACKS = '[data-testid="autoplay-stall-tracks"]';
const TRACK_ROW = '[data-testid="track-row"]';

/**
 * Raise a stall through the app's OWN module instance.
 *
 * Vite serves source modules from the same registry the app loaded, so this
 * import is the same singleton the mounted component reads - not a second copy
 * that would leave the banner untouched and the test passing for the wrong
 * reason.
 */
async function raiseStall(page: Page, blockedCount: number): Promise<void> {
	await page.evaluate(async (count) => {
		const store = await import(
			new URL('/src/lib/rb/autoplay-stall.svelte.ts', location.href).href
		);
		const pure = await import(new URL('/src/lib/rb/autoplay-stall.ts', location.href).href);
		store.raiseAutoPlayStall(
			pure.describeAutoPlayStall({
				reason: 'missing-audio',
				source_stable_id: 'browser-gate-source',
				blocked: Array.from({ length: count }, (_, index) => ({
					stable_id: `gone-${index}`,
					key: '8A',
					bpm: 124,
					file_exists: false,
					title: `Missing Track Number ${index}`,
					artist: `Artist ${index}`
				}))
			})
		);
	}, blockedCount);
}

async function clearStall(page: Page): Promise<void> {
	await page.evaluate(async () => {
		const store = await import(
			new URL('/src/lib/rb/autoplay-stall.svelte.ts', location.href).href
		);
		store.clearAutoPlayStall();
	});
}

/** The banner must not be pinned over the topbar it is meant to sit under. */
async function topbarBottom(page: Page): Promise<number> {
	const box = await page.locator('.rb-topbar').first().boundingBox();
	expect(box, 'the topbar did not render: nothing below is measurable').not.toBeNull();
	return box!.y + box!.height;
}

test.beforeEach(async ({ page }) => {
	test.setTimeout(120_000);
	await page.goto('/performance');
	// Past the preflight "Starting up" screen: the topbar only exists once the
	// real route mounted, so this is the readiness signal, not a fixed sleep.
	await expect(page.locator('.rb-topbar').first()).toBeVisible({ timeout: 90_000 });
	await clearStall(page);
});

test('a healthy performance page carries no stall banner', async ({ page }) => {
	// The control for every assertion below: if the banner were always in the
	// DOM, "it appeared" would prove nothing.
	await expect(page.locator(BANNER)).toHaveCount(0);
});

test('a stall raised after mount appears, visibly, under the topbar', async ({ page }) => {
	await raiseStall(page, 3);
	const banner = page.locator(BANNER);
	// toBeVisible, not toHaveCount: an element clipped to zero height by the
	// topbar's `overflow: hidden`, or painted behind the waveform stack, is
	// present in the markup and useless to the operator.
	await expect(banner).toBeVisible();
	await expect(banner).toContainText('all 3 remaining playlist tracks');
	await expect(banner).toContainText('Relink or re-download');

	const box = await banner.boundingBox();
	expect(box, 'a visible banner with no box is a contradiction').not.toBeNull();
	expect(box!.height).toBeGreaterThan(0);
	expect(box!.y).toBeGreaterThanOrEqual(await topbarBottom(page) - 1);
});

test('the banner leaves the DOM when the stall is retired', async ({ page }) => {
	await raiseStall(page, 2);
	await expect(page.locator(BANNER)).toBeVisible();
	await clearStall(page);
	// Gone, not merely hidden: a hidden banner still occupies the reader's
	// accessibility tree and would keep announcing a stop that is over.
	await expect(page.locator(BANNER)).toHaveCount(0);
});

test('the track list opens on click, and never covers the control that closes it', async ({
	page
}) => {
	await raiseStall(page, 15);
	const toggle = page.getByRole('button', { name: /Show the 15 tracks/ });
	await expect(toggle).toBeVisible();
	await expect(page.locator(TRACKS)).toHaveCount(0);

	await toggle.click();
	const list = page.locator(TRACKS);
	await expect(list).toBeVisible();
	await expect(list).toContainText('Artist 0 - Missing Track Number 0');
	// The cap is 12 of 15, and the remainder must be stated rather than
	// silently dropped.
	await expect(list.locator('li')).toHaveCount(13);
	await expect(list).toContainText('and 3 more');

	// The r3974518073 failure: a list positioned independently of the banner
	// covering the toggle. Playwright's click actionability re-checks hit-testing, so
	// a covered control fails here rather than in front of the operator.
	const hide = page.getByRole('button', { name: /Hide the 15 tracks/ });
	await expect(hide).toBeVisible();
	await hide.click();
	await expect(page.locator(TRACKS)).toHaveCount(0);
});

test('the banner does not obscure the deck controls beneath it', async ({ page }) => {
	const deckControl = page.locator('[data-testid="grid-adjust-deck-1"]').first();
	const before = await deckControl.boundingBox();
	// FAIL, never skip (Codex r3974819407). A null box means the one check that
	// answers "does the banner cover the decks" could not be MEASURED, and
	// reporting no failure for an unmeasured acceptance condition is the exact
	// defect .claude/rules/verification.md is about: a filter that promotes
	// anything unmeasured. If the control moves, this test must be repointed,
	// not quietly retired.
	expect(
		before,
		'deck 1 grid control has no box at this viewport, so the underlay check could not run'
	).not.toBeNull();

	await raiseStall(page, 15);
	await expect(page.locator(BANNER)).toBeVisible();
	await page.getByRole('button', { name: /Show the 15 tracks/ }).click();
	await expect(page.locator(TRACKS)).toBeVisible();

	// Still where it was, and still the topmost element at its own centre: the
	// banner is fixed and outside the grid, so it must not have reflowed the
	// decks NOR be painted over them.
	const after = await deckControl.boundingBox();
	expect(after, 'the deck control vanished when the banner appeared').not.toBeNull();
	expect(Math.round(after!.y)).toBe(Math.round(before!.y));
	const topmostIsDeckControl = await deckControl.evaluate((element) => {
		const box = element.getBoundingClientRect();
		const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
		return element === hit || element.contains(hit);
	});
	expect(topmostIsDeckControl, 'the stall list is painted over a deck control').toBe(true);
});

// ----- real exhaustion, end to end ----------------------------------------

type DisposablePlaylist = { name: string; playlistId: string; etag: string };

async function _dispatch(page: Page, command: unknown): Promise<unknown> {
	return page.evaluate((message) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.dispatch(message as never);
	}, command);
}

async function _query(page: Page): Promise<{ decks: Record<number, { duration_ms: number | null }> }> {
	return page.evaluate(() => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.query() as never;
	});
}

async function _visibleStableIds(page: Page): Promise<string[]> {
	return page.locator(TRACK_ROW).evaluateAll((rows) =>
		rows.map((row) => {
			const stableId = row.getAttribute('data-stable-id');
			if (stableId === null) throw new Error('visible track row has no stable id');
			return stableId;
		})
	);
}

async function _createDisposablePlaylist(
	page: Page,
	name: string,
	stableIds: readonly string[]
): Promise<DisposablePlaylist> {
	const created = await page.request.post('/api/v1/playlists', { data: { name } });
	expect(created.ok(), `create playlist failed: ${created.status()}`).toBe(true);
	const body = (await created.json()) as { playlist_id?: unknown };
	expect(typeof body.playlist_id).toBe('string');
	const createEtag = created.headers().etag;
	expect(createEtag, 'created playlist has no ETag').toBeTruthy();
	const replaced = await page.request.put(`/api/v1/playlists/${String(body.playlist_id)}/tracks`, {
		headers: { 'If-Match': createEtag },
		data: { stable_ids: stableIds }
	});
	expect(replaced.ok(), `replace playlist tracks failed: ${replaced.status()}`).toBe(true);
	const etag = replaced.headers().etag;
	expect(etag, 'updated playlist has no ETag').toBeTruthy();
	return { name, playlistId: String(body.playlist_id), etag };
}

test('a REAL exhaustion, driven end to end, puts the banner on screen', async ({ page }, info) => {
	test.setTimeout(180_000);
	await page.waitForFunction(() => window.musicDjToolsPerformance !== undefined);
	await page.getByText('All Tracks', { exact: true }).first().click();
	await expect(page.locator(TRACK_ROW).first()).toBeVisible({ timeout: 30_000 });
	const [onlyTrack] = await _visibleStableIds(page);
	expect(onlyTrack, 'the ingested fixture must expose at least one track').toBeTruthy();

	// ONE track in the playlist, and Enforce play order on, so the production
	// picker walks strict membership after the playing track and finds nothing.
	// That is a genuine terminal branch: no missing files, no injected state.
	const playlist = await _createDisposablePlaylist(
		page,
		`AutoPlay stall gate ${info.project.name} ${Date.now()}`,
		[onlyTrack]
	);
	try {
		await page.reload();
		await page.waitForFunction(() => window.musicDjToolsPerformance !== undefined);
		const autoPlay = page.getByRole('button', { name: 'AutoPlay', exact: true });
		if ((await autoPlay.getAttribute('aria-pressed')) === 'true') await autoPlay.click();
		await expect(autoPlay).toHaveAttribute('aria-pressed', 'false');

		await autoPlay.hover();
		const enforce = page
			.getByRole('dialog', { name: 'AutoPlay options' })
			.getByRole('checkbox', { name: 'Enforce play order' });
		await expect(enforce).toBeVisible();
		if (!(await enforce.isChecked())) await enforce.click();
		await expect(enforce).toBeChecked();

		const playlistRow = page.getByText(playlist.name, { exact: true });
		if (!(await playlistRow.isVisible())) {
			await page.locator('.row.folder').filter({ hasText: 'Playlists' }).click();
		}
		await playlistRow.click();
		await expect.poll(async () => (await _visibleStableIds(page)).join('\0')).toBe(onlyTrack);

		await autoPlay.click();
		await expect(autoPlay).toHaveAttribute('aria-pressed', 'true');
		await expect(page.locator(BANNER)).toHaveCount(0);

		// Real load, real master, real transport, real seek into the window.
		await _dispatch(page, { type: 'load', deck: 1, stable_id: onlyTrack });
		await _dispatch(page, { type: 'master', deck: 1 });
		await _dispatch(page, { type: 'play', deck: 1, playing: true });
		const duration = (await _query(page)).decks[1].duration_ms;
		expect(duration, 'the fixture track has no decoded duration').not.toBeNull();
		await _dispatch(page, { type: 'seek', deck: 1, position_ms: (duration as number) - 5_000 });

		// From here the production controller's own 250 ms poll does everything.
		const banner = page.locator(BANNER);
		await expect(banner).toBeVisible({ timeout: 60_000 });
		await expect(banner).toContainText('no next unplayed track in playlist order');
		await expect(banner).toContainText('Enforce play order');
	} finally {
		const deleted = await page.request.delete(`/api/v1/playlists/${playlist.playlistId}`, {
			headers: { 'If-Match': playlist.etag }
		});
		expect(deleted.ok(), `delete playlist failed: ${deleted.status()}`).toBe(true);
	}
});
