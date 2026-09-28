/**
 * Production-controller acceptance for exhausted AutoPlay playlist switches.
 *
 * Runs last by filename because each case deliberately drives decoded audio
 * to its exhaustion window. Keeping that real-time work after the long-lived
 * WebKit transport suite prevents one acceptance from perturbing another
 * without weakening or skipping either gate.
 *
 * Requirements:
 *
 * - ✔︎ Exercise the production artifact and public performance IPC.
 * - ✔︎ Create, populate, open, and delete playlists through real HTTP APIs.
 * - ✔︎ Prove smart and ordered controllers re-arm after real exhaustion.
 *
 * Acceptance tests:
 *
 * - [if] exhaustion remains armed after the visible playlist changes [then ⛔️]
 *   the replacement track never loads and the corresponding mode fails.
 * - [if] a test leaves its disposable playlists behind [then ⛔️] cleanup fails.
 */
import { expect, test, type Page } from '@playwright/test';

import type {
	PerformanceCommand,
	PerformanceState
} from '../../src/lib/rb/performance-ipc.svelte';

const PREFS_STORAGE_KEY = 'mdt.rb.ui-prefs.v1';
const TRACK_ROW = '[data-testid="track-row"]';

type DisposablePlaylist = {
	name: string;
	playlistId: string;
	etag: string;
};

async function _waitForIpc(page: Page): Promise<void> {
	await page.waitForFunction(() => window.musicDjToolsPerformance !== undefined);
}

async function _query(page: Page): Promise<PerformanceState> {
	return page.evaluate(() => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.query();
	});
}

async function _dispatch(page: Page, command: PerformanceCommand): Promise<PerformanceState> {
	return page.evaluate((message) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.dispatch(message);
	}, command);
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

async function _createDisposablePlaylist(
	page: Page,
	name: string,
	stableIds: readonly string[]
): Promise<DisposablePlaylist> {
	const created = await page.request.post('/api/v1/playlists', { data: { name } });
	if (!created.ok()) {
		throw new Error(`create playlist failed (${created.status()}): ${await created.text()}`);
	}
	const body = (await created.json()) as { playlist_id?: unknown };
	if (typeof body.playlist_id !== 'string' || body.playlist_id === '') {
		throw new Error('create playlist response has no playlist_id');
	}
	const createEtag = created.headers().etag;
	if (createEtag === undefined || createEtag === '') {
		throw new Error(`created playlist ${body.playlist_id} has no ETag`);
	}
	const replaced = await page.request.put(`/api/v1/playlists/${body.playlist_id}/tracks`, {
		headers: { 'If-Match': createEtag },
		data: { stable_ids: stableIds }
	});
	if (!replaced.ok()) {
		throw new Error(`replace playlist tracks failed (${replaced.status()}): ${await replaced.text()}`);
	}
	const etag = replaced.headers().etag;
	if (etag === undefined || etag === '') {
		throw new Error(`updated playlist ${body.playlist_id} has no ETag`);
	}
	return { name, playlistId: body.playlist_id, etag };
}

async function _deleteDisposablePlaylist(
	page: Page,
	playlist: DisposablePlaylist
): Promise<void> {
	const deleted = await page.request.delete(`/api/v1/playlists/${playlist.playlistId}`, {
		headers: { 'If-Match': playlist.etag }
	});
	if (!deleted.ok()) {
		throw new Error(`delete playlist failed (${deleted.status()}): ${await deleted.text()}`);
	}
}

async function _openPlaylist(page: Page, name: string, expectedIds: readonly string[]): Promise<void> {
	const row = page.getByText(name, { exact: true });
	if (!(await row.isVisible())) {
		await page.locator('.row.folder').filter({ hasText: 'Playlists' }).click();
	}
	await row.click();
	await expect.poll(async () => (await _visibleStableIds(page)).join('\0')).toBe(expectedIds.join('\0'));
}

async function _setEnforceOrder(page: Page, enabled: boolean): Promise<void> {
	const autoPlay = page.getByRole('button', { name: 'AutoPlay', exact: true });
	await autoPlay.hover();
	const option = page
		.getByRole('dialog', { name: 'AutoPlay options' })
		.getByRole('checkbox', { name: 'Enforce play order' });
	await expect(option).toBeVisible();
	if ((await option.isChecked()) !== enabled) await option.click();
	await expect(option).toBeChecked({ checked: enabled });
}

test.beforeEach(async ({ page }) => {
	await page.addInitScript((prefsKey) => {
		window.localStorage.setItem(prefsKey, JSON.stringify({ hide_broken_links: false }));
	}, PREFS_STORAGE_KEY);
	await page.goto('/performance');
	await _waitForIpc(page);
	await _openAllTracks(page);
});

for (const [mode, enforceOrder] of [
	['smart', false],
	['ordered', true]
] as const) {
	test(`AutoPlay ${mode}: a playlist switch re-arms an exhausted production controller`, async ({
		page
	}, testInfo) => {
		test.setTimeout(60_000);
		const [sourceId, targetId] = await _visibleStableIds(page);
		if (sourceId === undefined || targetId === undefined) {
			throw new Error('the ingested real-audio fixture must expose two tracks');
		}
		const suffix = `${testInfo.project.name}-${mode}-${Date.now()}`;
		const sourcePlaylist = await _createDisposablePlaylist(
			page,
			`AutoPlay exhausted source ${suffix}`,
			[sourceId]
		);
		const targetPlaylist = await _createDisposablePlaylist(
			page,
			`AutoPlay replacement target ${suffix}`,
			[targetId]
		);

		try {
			await page.reload();
			await _waitForIpc(page);
			const autoPlay = page.getByRole('button', { name: 'AutoPlay', exact: true });
			if ((await autoPlay.getAttribute('aria-pressed')) === 'true') await autoPlay.click();
			await expect(autoPlay).toHaveAttribute('aria-pressed', 'false');
			await _setEnforceOrder(page, enforceOrder);
			await _openPlaylist(page, sourcePlaylist.name, [sourceId]);
			await autoPlay.click();
			await expect(autoPlay).toHaveAttribute('aria-pressed', 'true');

			await _dispatch(page, { type: 'load', deck: 1, stable_id: sourceId });
			await _dispatch(page, { type: 'master', deck: 1 });
			await _dispatch(page, { type: 'play', deck: 1, playing: true });
			const loadedSource = (await _query(page)).decks[1];
			if (loadedSource.duration_ms === null) throw new Error('source fixture has no duration');
			await _dispatch(page, {
				type: 'seek',
				deck: 1,
				position_ms: loadedSource.duration_ms - 5_000
			});

			await expect
				.poll(
					() =>
						page.evaluate(
							(expectedMessage) =>
								window.musicDjToolsPerformance
									?.toasts()
									.some((toast) => toast.message.includes(expectedMessage)) ?? false,
							enforceOrder
								? 'no next unplayed track in playlist order'
								: 'no unplayed playlist track within key'
						),
					{ message: 'the production controller never recorded source-playlist exhaustion' }
				)
				.toBe(true);
			expect((await _query(page)).decks[2].stable_id).toBeNull();

			await _openPlaylist(page, targetPlaylist.name, [targetId]);
			await expect
				.poll(async () => {
					const follower = (await _query(page)).decks[2];
					return `${follower.stable_id ?? ''}:${follower.playing}`;
				}, { message: 'the production controller did not load and start the replacement feed' })
				.toBe(`${targetId}:true`);
		} finally {
			await _deleteDisposablePlaylist(page, targetPlaylist);
			await _deleteDisposablePlaylist(page, sourcePlaylist);
		}
	});
}
