/**
 * User-visible playlist edit undo/redo (#172).
 *
 * Creates a disposable playlist through the real write API, then undoes and
 * redoes mixed edits in the history panel, via Cmd/Ctrl+Z, after a reload,
 * and through the performance IPC.
 */
import { expect, test, type Page } from '@playwright/test';

import type { PerformanceCommand } from '../../src/lib/rb/performance-ipc.svelte';

const PREFS_STORAGE_KEY = 'mdt.rb.ui-prefs.v1';
const TRACK_ROW = '[data-testid="track-row"]';
const PLAYLIST_ROW = '[data-testid="playlist-row"]';

async function _waitForIpc(page: Page): Promise<void> {
	await page.waitForFunction(() => window.musicDjToolsPerformance !== undefined);
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

async function _openAllTracks(page: Page): Promise<void> {
	await page.getByText('All Tracks', { exact: true }).first().click();
	await expect(page.locator(TRACK_ROW).first()).toBeVisible({ timeout: 30_000 });
}

function _playlistRow(page: Page, name: string) {
	return page.locator(PLAYLIST_ROW).filter({ has: page.getByText(name, { exact: true }) });
}

async function _ensurePlaylistsOpen(page: Page): Promise<void> {
	const folder = page.getByTestId('playlist-folder');
	const disclosure = folder.locator('.disclosure');
	if (!(await disclosure.evaluate((el) => el.classList.contains('open')))) {
		await folder.click();
	}
}

async function _openPlaylist(page: Page, name: string): Promise<void> {
	await _ensurePlaylistsOpen(page);
	const row = _playlistRow(page, name);
	await expect(row).toBeVisible({ timeout: 15_000 });
	await row.click();
}

test.beforeEach(async ({ page }) => {
	await page.addInitScript((prefsKey) => {
		window.localStorage.setItem(prefsKey, JSON.stringify({ hide_broken_links: false }));
	}, PREFS_STORAGE_KEY);
	await page.goto('/performance');
	await _waitForIpc(page);
	await _openAllTracks(page);
});

test('playlist undo/redo: mixed edits, panel, hotkey, reload, IPC', async ({ page }) => {
	test.setTimeout(90_000);
	const ids = (await _visibleStableIds(page)).slice(0, 2);
	expect(ids.length, 'fixture library must have two tracks').toBeGreaterThanOrEqual(2);
	const stamp = Date.now();
	const originalName = `UndoRedo ${stamp}`;
	const renamedName = `UndoRedo Peak ${stamp}`;

	const created = await page.request.post('/api/v1/playlists', { data: { name: originalName } });
	expect(created.ok(), await created.text()).toBeTruthy();
	const createdBody = (await created.json()) as { playlist_id: string };
	const playlistId = createdBody.playlist_id;
	const createEtag = created.headers().etag;
	expect(createEtag).toBeTruthy();

	const replaced = await page.request.put(`/api/v1/playlists/${playlistId}/tracks`, {
		headers: { 'If-Match': createEtag as string },
		data: { stable_ids: ids }
	});
	expect(replaced.ok(), await replaced.text()).toBeTruthy();
	const rename = await page.request.patch(`/api/v1/playlists/${playlistId}`, {
		headers: { 'If-Match': replaced.headers().etag as string },
		data: { name: renamedName }
	});
	expect(rename.ok(), await rename.text()).toBeTruthy();

	const undoBtn = page.getByTestId('playlist-undo');
	const redoBtn = page.getByTestId('playlist-redo');
	await expect
		.poll(async () => {
			const hist = await page.request.get('/api/v1/playlist-history');
			expect(hist.ok(), await hist.text()).toBeTruthy();
			const body = (await hist.json()) as { can_undo?: unknown };
			return body.can_undo === true;
		})
		.toBe(true);
	await expect(undoBtn).toBeEnabled({ timeout: 15_000 });
	await expect
		.poll(async () => {
			const listed = await page.request.get('/api/v1/playlists');
			expect(listed.ok(), await listed.text()).toBeTruthy();
			const body = (await listed.json()) as Array<{ name?: unknown }>;
			return body.map((row) => (typeof row.name === 'string' ? row.name : ''));
		})
		.toContain(renamedName);
	await _ensurePlaylistsOpen(page);
	await expect(_playlistRow(page, renamedName)).toBeVisible({ timeout: 15_000 });

	await _openPlaylist(page, renamedName);
	await expect.poll(async () => (await _visibleStableIds(page)).join('\0')).toBe(ids.join('\0'));

	await undoBtn.click();
	await expect(page.getByText(originalName, { exact: true }).first()).toBeVisible();
	await _openPlaylist(page, originalName);
	await expect.poll(async () => (await _visibleStableIds(page)).join('\0')).toBe(ids.join('\0'));

	await page.getByTestId('playlist-history-list').click();
	await page.keyboard.press('Control+z');
	await expect.poll(async () => (await _visibleStableIds(page)).join('\0')).toBe('');

	await expect(redoBtn).toBeEnabled();
	await redoBtn.click();
	await expect.poll(async () => (await _visibleStableIds(page)).join('\0')).toBe(ids.join('\0'));

	const histBeforeReload = await page.request.get('/api/v1/playlist-history');
	expect(histBeforeReload.ok()).toBeTruthy();
	const beforeBody = (await histBeforeReload.json()) as {
		can_undo: boolean;
		can_redo: boolean;
		cursor: number;
		entries: Array<{ op: string }>;
	};

	await page.reload();
	await _waitForIpc(page);
	await expect(undoBtn).toBeEnabled({ timeout: 15_000 });
	const histAfterReload = await page.request.get('/api/v1/playlist-history');
	const afterBody = (await histAfterReload.json()) as {
		can_undo: boolean;
		can_redo: boolean;
		cursor: number;
		entries: Array<{ op: string }>;
	};
	expect(afterBody.can_undo).toBe(beforeBody.can_undo);
	expect(afterBody.can_redo).toBe(beforeBody.can_redo);
	expect(afterBody.cursor).toBe(beforeBody.cursor);
	expect(afterBody.entries.map((e) => e.op)).toEqual(beforeBody.entries.map((e) => e.op));

	await page.evaluate((command: PerformanceCommand) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.dispatch(command);
	}, { type: 'playlist_undo' } satisfies PerformanceCommand);
	await _openPlaylist(page, originalName);
	await expect.poll(async () => (await _visibleStableIds(page)).join('\0')).toBe('');

	const detail = await page.request.get(`/api/v1/playlists/${playlistId}`);
	const etag = detail.headers().etag;
	if (etag) {
		await page.request.delete(`/api/v1/playlists/${playlistId}`, {
			headers: { 'If-Match': etag }
		});
	}
});
