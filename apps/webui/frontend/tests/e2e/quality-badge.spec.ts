/**
 * QualityBadge contract, mocked at the NETWORK boundary (page.route) so the
 * assertions are about what the component renders for a given payload, not
 * about whatever the live library happens to hold today.
 *
 * One line of intent per test.
 */
import { expect, test } from '@playwright/test';

const SID = '0'.repeat(40);

const TRACK = {
	stable_id: SID,
	title: 'Boundary Test',
	artist: 'Effective Bitrate',
	album: null,
	duration_ms: 180_000,
	bpm: 128,
	key: '8A',
	rating: 4,
	tags: [],
	notes: null,
	last_played_at: null,
	file_path: '/music/boundary.mp3',
	created_at: '2026-07-24T00:00:00Z',
	updated_at: '2026-07-24T00:00:00Z',
	provenance: {}
};

const RB_META_BASE = {
	stable_id: SID,
	vendor: 'rekordbox',
	vendor_id: '1',
	folder_path: '/music/boundary.mp3',
	file_exists: true,
	is_streaming: false,
	genre: 'Techno',
	comment: null,
	duration_s: 180,
	artwork_available: false,
	analysis_available: false,
	cue_count: 0
};

async function mockTrackWithQuality(page, quality: Record<string, unknown>) {
	await page.route(`**/api/v1/tracks/${SID}`, (route) =>
		route.fulfill({
			status: 200,
			headers: { 'content-type': 'application/json', etag: '"abc"' },
			body: JSON.stringify(TRACK)
		})
	);
	await page.route(`**/api/v1/tracks/${SID}/rb-meta`, (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({ ...RB_META_BASE, quality })
		})
	);
}

test('renders the venue label plus container, with kbps in the hover title', async ({ page }) => {
	await mockTrackWithQuality(page, {
		venue: 'warehouse',
		label: 'Warehouse',
		rank: 4,
		of: 6,
		blurb: '320 kbps or better -- the practical ceiling for lossy',
		kbps: 319,
		container: '.mp3',
		lossless: false
	});
	await page.goto(`/track/${SID}`);
	const badge = page.locator('.q-badge');
	await expect(badge).toBeVisible();
	await expect(badge).toHaveAttribute('data-venue', 'warehouse');
	await expect(badge).toContainText('Warehouse');
	await expect(badge).toContainText('mp3');
	const title = await badge.getAttribute('title');
	expect(title).toContain('320 kbps or better');
	expect(title).toContain('Effective 319 kbps');
	expect(title).toContain('mp3');
	expect(title).toContain('Rung 5 of 6');
});

test('lossless renders the top rung and says so in the title', async ({ page }) => {
	await mockTrackWithQuality(page, {
		venue: 'stadium',
		label: 'Stadium',
		rank: 5,
		of: 6,
		blurb: 'lossless -- nothing thrown away, safe anywhere',
		kbps: 900,
		container: '.flac',
		lossless: true
	});
	await page.goto(`/track/${SID}`);
	const badge = page.locator('.q-badge');
	await expect(badge).toHaveAttribute('data-venue', 'stadium');
	await expect(badge).toContainText('Stadium');
	expect(await badge.getAttribute('title')).toContain('set by format, not bitrate');
});

test('an unmeasurable file renders as Unknown with the reason, never a guessed rung', async ({
	page
}) => {
	await mockTrackWithQuality(page, {
		venue: null,
		label: 'Unknown',
		rank: null,
		of: 6,
		blurb: 'file missing',
		kbps: null,
		container: '.mp3',
		lossless: false
	});
	await page.goto(`/track/${SID}`);
	const badge = page.locator('.q-badge');
	await expect(badge).toHaveAttribute('data-venue', 'unknown');
	await expect(badge).toContainText('Unknown');
	const title = await badge.getAttribute('title');
	expect(title).toContain('Quality unknown');
	expect(title).toContain('file missing');
	// No rung may leak into an unknown badge.
	expect(title).not.toContain('Rung');
	for (const rung of ['Lounge', 'Club', 'Warehouse', 'Stadium', 'Naughty']) {
		await expect(badge).not.toContainText(rung);
	}
});

test('the ladder endpoint is the legend source of truth, six rungs ascending', async ({
	request
}) => {
	const res = await request.get('/api/v1/tracks/quality-ladder');
	expect(res.status()).toBe(200);
	const rungs = (await res.json()) as { rank: number; key: string }[];
	expect(rungs.map((r) => r.rank)).toEqual([0, 1, 2, 3, 4, 5]);
	expect(rungs.map((r) => r.key)).toEqual([
		'naughty_step',
		'lounge',
		'house_party',
		'club',
		'warehouse',
		'stadium'
	]);
});
