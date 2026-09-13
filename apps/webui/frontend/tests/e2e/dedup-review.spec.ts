/**
 * Headed e2e for duplicate review + merge (#175).
 *
 * Not in the machine-enforced E2E workflow. Drive /dedup against the claimed
 * worktree daemon or the Playwright fixture engine. Never log member.path.
 *
 * Acceptance:
 * - [if] clusters are missing, skip with a message that names the missing db
 * - [if] the first cluster's two members lack title/artist/bpm then stop
 * - [if] /dedup member cards do not show those title/artist/bpm values then stop
 * - [if] keep-all / skip + reload + stale If-Match do not persist then stop
 * - [if] merge does not rewrite a throwaway playlist alias then stop
 * - [if] undo does not restore membership then stop
 */
import { expect, test, type APIRequestContext } from '@playwright/test';

interface ClusterMember {
	stable_id: string;
	title: string | null;
	artist: string | null;
	bpm: number | null;
}

interface ClusterRow {
	cluster_id: number;
	cluster_key: string;
	survivor_stable_id: string;
	members: ClusterMember[];
	decision: {
		action: string;
		survivor: string;
		pending_apply: boolean;
	} | null;
}

interface ClustersBody {
	clusters: ClusterRow[];
	revision: string;
	note: string | null;
}

async function _clusters(request: APIRequestContext): Promise<{
	status: number;
	body: ClustersBody | null;
	etag: string;
}> {
	const response = await request.get('/api/v1/dedup/clusters');
	const etag = response.headers()['etag'] ?? '';
	if (!response.ok()) {
		return { status: response.status(), body: null, etag };
	}
	return { status: response.status(), body: (await response.json()) as ClustersBody, etag };
}

test('dedup review: keep-all/skip persistence, merge apply/undo, conflict', async ({
	page
}) => {
	const first = await _clusters(page.request);
	if (first.body === null || first.body.clusters.length === 0) {
		const note = first.body?.note ?? `GET /api/v1/dedup/clusters status ${first.status}`;
		test.skip(true, `no duplicate clusters: ${note}`);
		return;
	}

	const cluster = first.body.clusters[0];
	expect(cluster.members.length).toBeGreaterThanOrEqual(2);
	const hydratedMembers = cluster.members.slice(0, 2);
	for (const member of hydratedMembers) {
		expect(member.stable_id).toBeTruthy();
		expect(member.title).toBeTruthy();
		expect(member.artist).toBeTruthy();
		expect(member.bpm).toEqual(expect.any(Number));
	}
	const alias = cluster.members.find((member) => member.stable_id !== cluster.survivor_stable_id);
	expect(alias, 'cluster has no alias member').toBeTruthy();
	const aliasId = alias!.stable_id;
	const survivorId = cluster.survivor_stable_id;

	await page.goto('/dedup');
	await expect(page.getByTestId('dedup-cluster').first()).toBeVisible({ timeout: 30_000 });
	await expect(page.getByTestId('dedup-member')).toHaveCount(cluster.members.length);
	await expect(page.getByText('Merge rewrites OpenDJ playlist memberships')).toBeVisible();

	const clusterCard = page.getByTestId('dedup-cluster').first();
	const memberCards = clusterCard.getByTestId('dedup-member');
	for (let index = 0; index < hydratedMembers.length; index += 1) {
		const member = hydratedMembers[index];
		const card = memberCards.nth(index);
		await expect(card.locator('.member-title')).toHaveText(
			`${member.title} - ${member.artist}`
		);
		await expect(card.locator('.member-meta')).toContainText(`${member.bpm} BPM`);
	}
	await clusterCard.getByTestId('dedup-keep-all').click();
	await expect(clusterCard.locator('.badge.decided')).toContainText('keep-all');
	await page.reload();
	await expect(page.getByTestId('dedup-cluster').first().locator('.badge.decided')).toContainText(
		'keep-all'
	);

	const afterKeep = await _clusters(page.request);
	expect(afterKeep.body?.clusters[0]?.decision?.action).toBe('keep-all');
	const skipResponse = await page.request.post(
		`/api/v1/dedup/clusters/${cluster.cluster_id}/decision`,
		{
			headers: { 'If-Match': first.etag },
			data: {
				cluster_key: cluster.cluster_key,
				survivor: survivorId,
				action: 'skip'
			}
		}
	);
	expect(skipResponse.status()).toBe(409);

	const replaceKeep = await page.request.post(
		`/api/v1/dedup/clusters/${cluster.cluster_id}/decision`,
		{
			headers: { 'If-Match': afterKeep.etag },
			data: {
				cluster_key: cluster.cluster_key,
				survivor: survivorId,
				action: 'merge'
			}
		}
	);
	expect(replaceKeep.ok()).toBeTruthy();

	const throwawayName = `dedup-review-e2e-${Date.now()}`;
	const created = await page.request.post('/api/v1/playlists', {
		data: { name: throwawayName }
	});
	expect(created.ok()).toBeTruthy();
	const createdBody = (await created.json()) as { playlist_id: string };
	const playlistId = createdBody.playlist_id;
	const filled = await page.request.put(`/api/v1/playlists/${playlistId}/tracks`, {
		headers: { 'If-Match': created.headers()['etag'] ?? '' },
		data: { stable_ids: [aliasId] }
	});
	expect(filled.ok()).toBeTruthy();

	try {
		await page.goto('/dedup');
		await expect(page.getByTestId('dedup-cluster').first()).toBeVisible({ timeout: 30_000 });
		const mergeCard = page.getByTestId('dedup-cluster').first();
		page.once('dialog', (dialog) => dialog.accept());
		await mergeCard.getByTestId('dedup-merge').click();
		await expect(mergeCard.locator('.badge.decided')).toContainText(`applied: merge -> ${survivorId}`);
		await page.reload();
		await expect(
			page.getByTestId('dedup-cluster').first().locator('.badge.decided')
		).toContainText(`applied: merge -> ${survivorId}`);

		const afterApply = await page.request.get(`/api/v1/playlists/${playlistId}`);
		expect(afterApply.ok()).toBeTruthy();
		const afterApplyBody = (await afterApply.json()) as { items: string[] };
		expect(afterApplyBody.items).toEqual([survivorId]);

		await page.getByTestId('dedup-cluster').first().getByTestId('dedup-undo').click();
		await expect(
			page.getByTestId('dedup-cluster').first().locator('.badge.decided')
		).toContainText('pending apply');
		await page.reload();
		const restored = await page.request.get(`/api/v1/playlists/${playlistId}`);
		expect(restored.ok()).toBeTruthy();
		const restoredBody = (await restored.json()) as { items: string[] };
		expect(restoredBody.items).toEqual([aliasId]);

		const conflictProbe = await _clusters(page.request);
		const firstApply = await page.request.post(
			`/api/v1/dedup/clusters/${cluster.cluster_id}/apply`,
			{
				headers: { 'If-Match': conflictProbe.etag },
				data: { cluster_key: cluster.cluster_key, survivor: survivorId }
			}
		);
		expect(firstApply.ok()).toBeTruthy();
		await page.getByTestId('dedup-cluster').first().getByTestId('dedup-merge').click();
		await expect(page.locator('.post-error').first()).toBeVisible();
	} finally {
		const detail = await page.request.get(`/api/v1/playlists/${playlistId}`);
		const etag = detail.headers()['etag'];
		if (detail.ok() && etag) {
			await page.request.delete(`/api/v1/playlists/${playlistId}`, {
				headers: { 'If-Match': etag }
			});
		}
	}
});
