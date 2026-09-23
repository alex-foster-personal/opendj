// requirement: LIBUX-16
// [if] a folder is dropped on the playlist tree [then] a playlist named after it is created, new files are
// ingested and exact library duplicates are linked, both as members in folder order [else fail].
// [if] the same folder is dropped again [then] it succeeds into a fresh batch instead of a 409 [else fail].
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test, expect, type Page } from '@playwright/test';

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

test.describe('playlist folder drop', () => {
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
});
