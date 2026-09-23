// requirement: LIBUX-16
// [if] a folder is dropped on the playlist tree [then] a playlist named after it is created, new files are
// ingested and exact library duplicates are linked, both as members in folder order [else fail].
// [if] the same folder is dropped again [then] it succeeds into a fresh batch instead of a 409 [else fail].
// [if] a drop meets a refresh job already running [then] it reports success and its own batch refresh starts
// once the slot frees, while any other refresh error still fails the drop visibly [else fail].
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test, expect, type APIRequestContext, type Page } from '@playwright/test';

const FIXTURE_DIR = path.resolve(
	fileURLToPath(new URL('.', import.meta.url)),
	'../../../../../tests/fixtures/phase7-dedup'
);
// Real audio from the locked phase7 corpus. Durations (3.06 s vs 6.06 s) sit
// outside the duplicate-duration tolerance of each other and of the 60 s
// deckload fixture library, so the only duplicate candidate is the one the
// seed drop below puts into the library through this same UI path.
const DUP_SOURCE = 'other-silent-intro.mp3';
const NEW_SOURCE = 'src-128.mp3';

type DropFile = { name: string; b64: string };

function fixtureFile(name: string): DropFile {
	const full = path.join(FIXTURE_DIR, name);
	if (!fs.existsSync(full)) throw new Error(`locked fixture missing: ${full}`);
	return { name, b64: fs.readFileSync(full).toString('base64') };
}

/**
 * Drop real audio bytes on the playlist tree through the DOM drag events the
 * browser dispatches for an OS drag. Each File carries the folder-relative
 * path the folder walker produces, so the drop names its folder.
 */
async function dropFolderOnPlaylistTree(page: Page, folder: string, files: DropFile[]): Promise<void> {
	await page.evaluate(
		({ folder, files }) => {
			const target = document.querySelector('[data-testid="playlist-tree-panel"] .tree-scroll');
			if (target === null) throw new Error('playlist tree scroll area not rendered');
			const dt = new DataTransfer();
			for (const f of files) {
				const bytes = Uint8Array.from(atob(f.b64), (c) => c.charCodeAt(0));
				dt.items.add(new File([bytes], `${folder}/${f.name}`, { type: 'audio/mpeg' }));
			}
			target.dispatchEvent(new DragEvent('dragover', { dataTransfer: dt, bubbles: true, cancelable: true }));
			target.dispatchEvent(new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }));
		},
		{ folder, files }
	);
}

type Summary = { playlist_id: string; name: string };

async function playlistsNamed(page: Page, name: string): Promise<Summary[]> {
	const r = await page.request.get('/api/v1/playlists?availability=skip');
	expect(r.ok()).toBe(true);
	return ((await r.json()) as Summary[]).filter((p) => p.name === name);
}

async function playlistItems(page: Page, playlistId: string): Promise<string[]> {
	const r = await page.request.get(`/api/v1/playlists/${playlistId}`);
	expect(r.ok()).toBe(true);
	return ((await r.json()) as { items: string[] }).items;
}

/**
 * Wait until the drop's new playlist named `name` (one not in `before`) holds its members
 * (the playlist is created first and filled at the end of the drop). Without
 * chromaprint a library duplicate is only a duration match, which the drop
 * holds for a decision: answer it with Skip, which links the existing track.
 */
async function awaitDropPlaylist(page: Page, name: string, before: Summary[] = []): Promise<Summary> {
	const known = new Set(before.map((p) => p.playlist_id));
	const modal = page.getByTestId('playlist-folder-dup-modal');
	const deadline = Date.now() + 20_000;
	for (;;) {
		if (await modal.isVisible()) {
			for (const skip of await modal.getByTestId('ingest-dup-reject').all()) await skip.click();
			await modal.getByRole('button', { name: 'Continue' }).click();
		}
		const created = (await playlistsNamed(page, name)).filter((p) => !known.has(p.playlist_id));
		if (created.length > 0) {
			expect(created).toHaveLength(1);
			if ((await playlistItems(page, created[0].playlist_id)).length > 0) return created[0];
		}
		if (Date.now() > deadline) {
			throw new Error(`folder drop never filled a new playlist ${JSON.stringify(name)}`);
		}
		await page.waitForTimeout(250);
	}
}

/** Every live track id in the library, walked through the cursor pages. */
async function liveTrackIds(request: APIRequestContext): Promise<Set<string>> {
	const ids = new Set<string>();
	let cursor: string | null = null;
	do {
		const query: string = cursor === null ? '' : `&cursor=${encodeURIComponent(cursor)}`;
		const r = await request.get(`/api/v1/tracks?limit=1000${query}`);
		expect(r.ok(), await r.text()).toBe(true);
		const page = (await r.json()) as { items: { stable_id: string }[]; next_cursor: string | null };
		for (const t of page.items) ids.add(t.stable_id);
		cursor = page.next_cursor;
	} while (cursor !== null);
	return ids;
}

async function livePlaylistIds(request: APIRequestContext): Promise<Set<string>> {
	const r = await request.get('/api/v1/playlists?availability=skip');
	expect(r.ok(), await r.text()).toBe(true);
	return new Set(((await r.json()) as Summary[]).map((p) => p.playlist_id));
}

async function ingestStepsEnabled(request: APIRequestContext): Promise<Record<string, boolean>> {
	const r = await request.get('/api/v1/ingest/config');
	expect(r.ok(), await r.text()).toBe(true);
	const cfg = (await r.json()) as { steps: { id: string; enabled: boolean }[] };
	return Object.fromEntries(cfg.steps.map((s) => [s.id, s.enabled]));
}

type RefreshStatus = {
	running: boolean;
	phase: string;
	started_at: number | null;
	log_tail: string[];
};

async function refreshStatus(request: APIRequestContext): Promise<RefreshStatus> {
	const r = await request.get('/api/v1/ingest/refresh/status');
	expect(r.ok(), await r.text()).toBe(true);
	return (await r.json()) as RefreshStatus;
}

/** The toast tray's current text; toasts render into the one `.toast-stack`. */
function toastStack(page: Page) {
	return page.locator('.toast-stack');
}

type LibrarySnapshot = {
	tracks: Set<string>;
	playlists: Set<string>;
	steps: Record<string, boolean>;
};
let before: LibrarySnapshot | null = null;

test.describe('playlist folder drop', () => {
	// This spec runs inside the ROOT suite's one shared engine and library, so
	// whatever a drop adds is seen by every spec after it. Left behind, the
	// 3 s and 6 s fixture tracks joined the library rows that later specs load
	// by index, which then asserted 5 s and 20 s seeks inside a 3 s file
	// (preview-cue-library) and fed autoplay tracks with no beat grid, while
	// the drop's extra playlists and analysis job outlived the spec (PR #3683
	// e2e run 35833610668). So every test here hands the library back exactly
	// as it found it: playlists and tracks it created are removed, and no
	// refresh job is left running.
	test.beforeEach(async ({ request }) => {
		before = {
			tracks: await liveTrackIds(request),
			playlists: await livePlaylistIds(request),
			steps: await ingestStepsEnabled(request)
		};
	});

	test.afterEach(async ({ page, request }, testInfo) => {
		// The hook shares the test's budget; give the refresh wait its own.
		testInfo.setTimeout(testInfo.timeout + 75_000);
		// Close the page first: a drop that met a busy refresh slot queues its
		// batch refresh IN the page, and that poller must not start a job
		// after the idle wait below has already passed.
		await page.close();
		const snapshot = before;
		before = null;
		expect(snapshot, 'beforeEach did not snapshot the library').not.toBeNull();
		const put = await request.put('/api/v1/ingest/config', { data: { enabled: snapshot!.steps } });
		expect(put.ok(), await put.text()).toBe(true);
		for (const id of await livePlaylistIds(request)) {
			if (snapshot!.playlists.has(id)) continue;
			const detail = await request.get(`/api/v1/playlists/${id}`);
			expect(detail.ok(), await detail.text()).toBe(true);
			const etag = detail.headers().etag;
			expect(etag, `playlist ${id} detail carries no ETag`).toBeTruthy();
			const del = await request.delete(`/api/v1/playlists/${id}`, { headers: { 'If-Match': etag } });
			expect(del.ok(), await del.text()).toBe(true);
		}
		for (const id of await liveTrackIds(request)) {
			if (snapshot!.tracks.has(id)) continue;
			const removed = await request.post(`/api/v1/tracks/${id}:remove`);
			expect(removed.ok(), await removed.text()).toBe(true);
		}
		// Removal empties the analyze-on-import backlog; wait out any drain
		// already running over it so no analysis subprocess leaks forward.
		await expect
			.poll(
				async () => {
					const r = await request.get('/api/v1/ingest/refresh/status');
					expect(r.ok(), await r.text()).toBe(true);
					return ((await r.json()) as { running: boolean }).running;
				},
				{ timeout: 60_000, message: 'a refresh job the drop started never settled' }
			)
			.toBe(false);
		// Positive proof of the hand-back, not an absence of errors.
		expect(await livePlaylistIds(request)).toEqual(snapshot!.playlists);
		expect(await liveTrackIds(request)).toEqual(snapshot!.tracks);
		expect(await ingestStepsEnabled(request)).toEqual(snapshot!.steps);
	});

	test('file drag over playlist tree does not open generic ingest overlay', async ({ page }) => {
		await page.goto('/performance');
		await expect(page.getByTestId('refresh-analysis')).toBeVisible({ timeout: 15_000 });
		await expect(page.getByTestId('playlist-tree-panel')).toBeVisible();

		await page.evaluate(() => {
			const panel = document.querySelector('[data-testid="playlist-tree-panel"]');
			const dt = new DataTransfer();
			dt.items.add(new File([new Uint8Array(8)], 'junk.txt', { type: 'text/plain' }));
			panel?.dispatchEvent(
				new DragEvent('dragenter', { dataTransfer: dt, bubbles: true, cancelable: true })
			);
			panel?.dispatchEvent(
				new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true })
			);
		});

		await expect(page.getByTestId('ingest-drop-overlay')).not.toBeVisible();
		await expect(page.getByTestId('ingest-modal')).not.toBeVisible();
	});

	test('file drag elsewhere still opens ingest overlay', async ({ page }) => {
		await page.goto('/performance');
		await expect(page.getByTestId('refresh-analysis')).toBeVisible({ timeout: 15_000 });

		await page.evaluate(() => {
			const dt = new DataTransfer();
			dt.items.add(new File([new Uint8Array(4096).fill(65)], 'e2e-junk.mp3', { type: 'audio/mpeg' }));
			window.dispatchEvent(new DragEvent('dragenter', { dataTransfer: dt, bubbles: true }));
		});

		await expect(page.getByTestId('ingest-drop-overlay')).toBeVisible();
	});

	test('folder drop creates a named playlist of linked duplicates and new tracks, in folder order', async ({
		page
	}) => {
		test.setTimeout(90_000); // three real drops, each an upload + fingerprint + ingest
		const dup = fixtureFile(DUP_SOURCE);
		const fresh = fixtureFile(NEW_SOURCE);
		await page.goto('/performance');
		await expect(page.getByTestId('playlist-tree-panel')).toBeVisible({ timeout: 15_000 });

		// Seed: a first folder drop ingests DUP_SOURCE into the library.
		await dropFolderOnPlaylistTree(page, 'E2E Seed Folder', [dup]);
		const seed = await awaitDropPlaylist(page, 'E2E Seed Folder');
		const seedItems = await playlistItems(page, seed.playlist_id);
		expect(seedItems).toHaveLength(1);
		const existingId = seedItems[0];

		// Mixed: a new file first, then the library duplicate. Membership must
		// follow that folder order, not group duplicates ahead of new tracks.
		await dropFolderOnPlaylistTree(page, 'Agnes Obel', [fresh, dup]);
		const mixed = await awaitDropPlaylist(page, 'Agnes Obel');
		const items = await playlistItems(page, mixed.playlist_id);
		expect(items).toHaveLength(2);
		expect(items[0]).not.toBe(existingId); // newly ingested
		expect(items[1]).toBe(existingId); // linked, not re-imported
		await expect(page.getByTestId('ingest-modal')).not.toBeVisible();

		// Re-drop the same folder: a fresh batch, so no 409 on the files the
		// first drop left staged; both are library tracks now and get linked.
		await dropFolderOnPlaylistTree(page, 'Agnes Obel', [fresh, dup]);
		const again = await awaitDropPlaylist(page, 'Agnes Obel', [mixed]);
		expect(await playlistItems(page, again.playlist_id)).toEqual(items);
	});

	test('a drop that meets a running refresh succeeds and queues its own batch refresh', async ({
		page
	}) => {
		test.setTimeout(150_000); // two real drops, then the queued refresh's own run
		const refreshPosts: number[] = [];
		page.on('response', (r) => {
			if (r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/v1/ingest/refresh') {
				refreshPosts.push(r.status());
			}
		});
		await page.goto('/performance');
		await expect(page.getByTestId('playlist-tree-panel')).toBeVisible({ timeout: 15_000 });

		// Back to back, as a user drops two folders: the first drop's batch
		// refresh (a real analysis subprocess) still holds the one slot when
		// the second drop asks for its own.
		await dropFolderOnPlaylistTree(page, 'E2E Busy First', [fixtureFile(DUP_SOURCE)]);
		await awaitDropPlaylist(page, 'E2E Busy First');
		await dropFolderOnPlaylistTree(page, 'E2E Busy Second', [fixtureFile(NEW_SOURCE)]);
		await awaitDropPlaylist(page, 'E2E Busy Second');

		// The drop reports success, not "folder drop failed".
		await expect(toastStack(page)).toContainText('Created "E2E Busy Second"', { timeout: 20_000 });
		expect(
			refreshPosts,
			'precondition: the second drop never met a busy refresh slot, so this run proves nothing'
		).toContain(409);

		// The running job cannot see the second batch (a batch job enumerates
		// only its own batch_dir), so the queued request must start a job over
		// the second drop's batch once the slot frees, and that job must end.
		await expect
			.poll(
				async () => {
					const s = await refreshStatus(page.request);
					return (
						!s.running &&
						s.log_tail.some((l) => l.includes('batch scope:') && l.includes('E2E Busy Second-'))
					);
				},
				{ timeout: 100_000, message: 'no refresh ever ran over the second drop\'s batch' }
			)
			.toBe(true);
		expect(refreshPosts.slice(refreshPosts.indexOf(409))).toContain(202);
		await expect(toastStack(page)).not.toContainText('folder drop failed');
	});

	test('a refresh that fails for a real reason still fails the drop visibly', async ({ page }) => {
		// Control for the queued path above: only the busy-slot 409 is benign.
		// With every ingest step disabled the refresh endpoint answers 422
		// "no steps enabled", and that must still surface as a failed drop.
		expect(before, 'beforeEach did not snapshot the ingest config').not.toBeNull();
		const off = Object.fromEntries(Object.keys(before!.steps).map((id) => [id, false]));
		const put = await page.request.put('/api/v1/ingest/config', { data: { enabled: off } });
		expect(put.ok(), await put.text()).toBe(true);
		await page.goto('/performance');
		await expect(page.getByTestId('playlist-tree-panel')).toBeVisible({ timeout: 15_000 });

		await dropFolderOnPlaylistTree(page, 'E2E No Steps', [fixtureFile(NEW_SOURCE)]);
		await expect(toastStack(page)).toContainText('folder drop failed', { timeout: 30_000 });
		await expect(toastStack(page)).toContainText('no steps enabled');
		await expect(toastStack(page)).not.toContainText('Created "E2E No Steps"');
	});
});
