// requirement: KEYB-14215
/**
 * Issue #12286 (A11Y-01): the /performance hotkey listener across in-app
 * remounts. Gig -> Library -> Gig stays in one document, so the route mounts
 * twice on the same window. A listener the first mount failed to remove would
 * still be live there, and one Space press would then reach both: each reads
 * the same paused deck and each sends its own `play`, so the bus history shows
 * two commands for one press.
 *
 * The count comes from the bus itself (`query().history`, ids `pc-<seq>`), not
 * from deck state: two `play: true` commands leave the deck playing exactly as
 * one does, so a playing-state check could never see the duplicate.
 *
 * The unit file performance-hotkeys-remount.test.mjs drives the real install
 * function through mount, teardown, remount and a double install in node; this
 * file proves the same in the browser through real navigation.
 *
 * [if] /performance is mounted twice through navigation [then] one Space press sends one transport command
 * [if] the route unmounts [then] its former listener cannot dispatch a command
 * [if] the route mounts again [then] its shortcut remains available once per press
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

const API_BASE = process.env.PERFORMANCE_E2E_API_BASE ?? 'http://127.0.0.1:8686';
const SETTLE_MS = 750;
const TRANSPORT_TYPES: readonly string[] = ['play', 'load_play_intent'];

type HistoryEntry = { id: string; type: string };

async function firstOnDiskStableId(request: APIRequestContext): Promise<string> {
	const response = await request.get(`${API_BASE}/api/v1/tracks?limit=50&available=true`);
	expect(response.ok(), 'track listing must succeed').toBeTruthy();
	const payload = (await response.json()) as {
		items: { stable_id: string; file_exists: boolean }[];
	};
	const track = payload.items.find(
		(item) => item.file_exists && typeof item.stable_id === 'string' && item.stable_id.length > 0
	);
	expect(track, 'library must include at least one on-disk available track').toBeDefined();
	return track!.stable_id;
}

async function waitForPerformanceIpc(page: Page): Promise<void> {
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, {
		timeout: 60_000
	});
}

/** Leave Gig through the mode picker: a client-side navigation that unmounts /performance. */
async function leaveToLibraryInApp(page: Page): Promise<void> {
	const picker = page.locator('details.mode-picker');
	await picker.locator('summary[aria-label="Choose app mode"]').click();
	await picker
		.locator('a.mode-card[data-testid="mode-card"]')
		.filter({ hasText: 'Library' })
		.click();
	await page.waitForURL((url) => url.pathname === '/');
	await page.waitForFunction(() => window.__mdtLibraryModeIdle === true);
}

/** Back to Gig through the Library layout's own Performance link, in the same document. */
async function returnToGigInApp(page: Page): Promise<void> {
	await page.getByRole('link', { name: 'Performance', exact: true }).click();
	await page.waitForURL((url) => url.pathname === '/performance');
	await waitForPerformanceIpc(page);
}

function sequenceOf(entry: HistoryEntry): number {
	const match = /^pc-(\d+)$/.exec(entry.id);
	if (match === null) throw new Error(`unexpected bus history id ${entry.id}`);
	return Number(match[1]);
}

async function readHistory(page: Page): Promise<HistoryEntry[]> {
	return page.evaluate(() => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC missing');
		return ipc.query().history.map((entry) => ({ id: entry.id, type: entry.type }));
	});
}

/** Highest bus sequence so far (0 before any command): the mark later counts start after. */
async function busMark(page: Page): Promise<number> {
	return Math.max(0, ...(await readHistory(page)).map(sequenceOf));
}

/** Transport commands the bus recorded after `mark`. */
async function transportCommandsSince(page: Page, mark: number): Promise<HistoryEntry[]> {
	return (await readHistory(page)).filter(
		(entry) => sequenceOf(entry) > mark && TRANSPORT_TYPES.includes(entry.type)
	);
}

async function loadDeckOne(page: Page, stableId: string): Promise<void> {
	await page.evaluate(async (sid) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC missing');
		await ipc.dispatch({ type: 'load', deck: 1, stable_id: sid });
	}, stableId);
	await page.waitForFunction(
		(sid) => window.musicDjToolsPerformance?.query().decks[1].stable_id === sid,
		stableId,
		{ timeout: 90_000 }
	);
}

/** Space on page chrome (body focused), which is where the transport shortcut lives. */
async function pressSpaceOnBody(page: Page): Promise<void> {
	await page.evaluate(() => {
		if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
	});
	expect(await page.evaluate(() => document.activeElement === document.body)).toBe(true);
	await page.keyboard.press('Space');
}

/**
 * One Space press, then the transport commands it sent once the bus settles.
 * Waits for the first so a slow bus cannot pass as "zero", then holds a
 * settle window so a second, duplicate dispatch has time to land and be seen.
 */
async function transportCommandsForOneSpace(page: Page): Promise<HistoryEntry[]> {
	const mark = await busMark(page);
	await pressSpaceOnBody(page);
	await expect
		.poll(async () => (await transportCommandsSince(page, mark)).length, {
			message: 'if no transport command arrives then the remounted route lost its Space shortcut',
			timeout: 10_000
		})
		.toBeGreaterThan(0);
	await page.waitForTimeout(SETTLE_MS);
	return transportCommandsSince(page, mark);
}

async function markDocument(page: Page): Promise<void> {
	await page.evaluate(() => {
		(window as unknown as { __hotkeysRemountMarker?: number }).__hotkeysRemountMarker = 1;
	});
}

async function expectSameDocument(page: Page): Promise<void> {
	expect(
		await page.evaluate(
			() => (window as unknown as { __hotkeysRemountMarker?: number }).__hotkeysRemountMarker
		),
		'if the marker is gone then the navigation reloaded the page and the route never remounted in place'
	).toBe(1);
}

test('performance: after two in-app mounts one Space press sends one transport command', async ({
	page,
	request
}) => {
	test.setTimeout(180_000);
	const stableId = await firstOnDiskStableId(request);
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await waitForPerformanceIpc(page);
	await markDocument(page);

	// Control on the first mount: the probe sees exactly one command per press.
	await loadDeckOne(page, stableId);
	const firstMount = await transportCommandsForOneSpace(page);
	expect(firstMount.map((entry) => entry.type)).toEqual(['play']);

	await leaveToLibraryInApp(page);
	await returnToGigInApp(page);
	await expectSameDocument(page);

	await loadDeckOne(page, stableId);
	const secondMount = await transportCommandsForOneSpace(page);
	expect(
		secondMount.map((entry) => entry.type),
		'if two commands arrive then the first mount left its keydown listener on the window'
	).toEqual(['play']);
});

test('performance: an unmounted route sends nothing on Space, and the remount answers once per press', async ({
	page,
	request
}) => {
	test.setTimeout(180_000);
	const stableId = await firstOnDiskStableId(request);
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await waitForPerformanceIpc(page);
	await markDocument(page);
	await loadDeckOne(page, stableId);

	const beforeLeaving = await busMark(page);
	await leaveToLibraryInApp(page);
	await page.evaluate(() => {
		if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
	});
	await page.keyboard.press('Space');
	await page.waitForTimeout(SETTLE_MS);
	await returnToGigInApp(page);
	await expectSameDocument(page);
	expect(
		await transportCommandsSince(page, beforeLeaving),
		'if a transport command was recorded while Library was showing then the unmounted listener dispatched'
	).toEqual([]);

	await loadDeckOne(page, stableId);
	const counts: number[] = [];
	for (let press = 0; press < 3; press += 1) {
		counts.push((await transportCommandsForOneSpace(page)).length);
	}
	expect(counts, 'every press on the remounted route sends exactly one command').toEqual([1, 1, 1]);
});
