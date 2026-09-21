// requirement: OBS-06
/**
 * Session replay through the REAL Sentry loader SDK (OBS-06), against the real
 * engine and the production page code: no fake SDK anywhere.
 *
 * What it proves, in order:
 *   1. after acceptance the production `startReplay` loads the loader from the
 *      CDN, the SDK initializes, and replay segments reach Sentry's ingest
 *      (an envelope POST is observed);
 *   2. pressing play on a deck (the real live-transport flip) stops the
 *      replay through the production watcher: `getReplayId()` reads undefined
 *      and NO further envelope leaves for longer than the SDK's flush interval,
 *      which is what `stop({ forceFlush: false })` discarding the tail means;
 *   3. after the deck stops, the poll restarts the replay and uploads resume.
 *
 * UNAVAILABLE, not a fake pass, whenever the engine has no live client or no
 * frontend DSN (every CI run today): the consent route then withholds the
 * loader URL. To run it for real, export OPENDJ_TELEMETRY=1, SENTRY_DSN and
 * SENTRY_FRONTEND_DSN for the engine before `pnpm test:e2e`, on a host that
 * can reach js-*.sentry-cdn.com. It sends one short replay to the fe project.
 */
import { expect, test, type Page } from '@playwright/test';

const INGEST = /ingest\.(?:[a-z]+\.)?sentry\.io\/api\/\d+\/envelope/;
const FLUSH_INTERVAL_MS = 5_500; // Sentry replay flushMaxDelay default
const TRACK_ROW = '[data-testid="track-row"]';

async function replayId(page: Page): Promise<string | undefined> {
	return page.evaluate(() => {
		const w = window as unknown as {
			Sentry?: { getReplay?: () => { getReplayId?: () => string | undefined } | undefined };
		};
		return w.Sentry?.getReplay?.()?.getReplayId?.();
	});
}

test('real loader: replay uploads after consent, stops and discards on play, resumes after stop', async ({
	page
}) => {
	test.setTimeout(120_000);
	const before = await (await page.request.get('/api/v1/telemetry/consent')).json();
	test.skip(
		before.replay_loader_url === null || before.telemetry_active !== true,
		'UNAVAILABLE: the engine has no live Sentry client or no frontend DSN, so the consent route withholds the loader URL (export OPENDJ_TELEMETRY=1, SENTRY_DSN, SENTRY_FRONTEND_DSN to run this against the real loader)'
	);
	const accepted = await page.request.put('/api/v1/telemetry/consent', {
		data: { decision: 'accepted', terms_version: before.terms_current_version }
	});
	expect(accepted.ok()).toBe(true);

	// Every envelope that leaves, with its item types parsed from the envelope
	// header lines, so a failure names WHAT left (replay_event, session,
	// client_report, event), not just that something did.
	const envelopes: Array<{ at: number; types: string[] }> = [];
	page.on('request', (r) => {
		if (r.method() !== 'POST' || !INGEST.test(r.url())) return;
		const body = r.postDataBuffer()?.toString('latin1') ?? '';
		const types: string[] = [];
		for (const line of body.split('\n')) {
			if (!line.startsWith('{')) continue;
			try {
				const parsed = JSON.parse(line) as { type?: string };
				if (typeof parsed.type === 'string') types.push(parsed.type);
			} catch {
				/* a payload line, not a header */
			}
		}
		envelopes.push({ at: Date.now(), types });
	});

	await page.goto('/');
	await page.waitForFunction(() => (window as { __mdtPerfLog?: unknown }).__mdtPerfLog !== undefined);
	// 1. the production startReplay injected the loader tag (this PR's code),
	// and the CDN answered (the network's). Tell the two apart: a tag with no
	// SDK behind it after 45 s is the CDN, reported UNAVAILABLE, never a verdict.
	await expect
		.poll(() => page.locator('script[src*="sentry-cdn.com"]').count(), { timeout: 30_000 })
		.toBeGreaterThan(0);
	const sdkUp = await page
		.waitForFunction(() => (window as { Sentry?: unknown }).Sentry !== undefined, null, {
			timeout: 45_000
		})
		.then(() => true)
		.catch(() => false);
	test.skip(
		!sdkUp,
		'UNAVAILABLE: the loader tag was injected but the Sentry CDN did not deliver the SDK within 45 s (network), so replay behavior cannot be measured here'
	);
	await expect.poll(() => replayId(page), { timeout: 60_000 }).toMatch(/^[0-9a-f]{32}$/);
	await expect.poll(() => envelopes.length, { timeout: 30_000 }).toBeGreaterThan(0);

	// 2. a deck goes live: load a track and press play, as a user would.
	const row = page.locator(TRACK_ROW).first();
	await row.locator('td.c-title').click();
	await row.locator('td.c-title').hover();
	await row.locator('button[title="Load onto deck 1"]').click();
	await expect(page.locator('[data-testid="play-deck-1"]')).toBeEnabled({ timeout: 30_000 });
	await page.waitForTimeout(1_500);
	await page.locator('[data-testid="play-deck-1"]').click();
	await expect
		.poll(() => replayId(page), { timeout: 5_000 })
		.toBeUndefined();
	const sentBeforeStop = envelopes.length;
	await page.waitForTimeout(FLUSH_INTERVAL_MS * 2);
	const leaked = envelopes.slice(sentBeforeStop).map((e) => e.types.join('+') || '(unparsed)');
	expect(
		leaked,
		'no envelope may leave while the deck is live: the stop discarded its tail and no flush timer fired'
	).toEqual([]);

	// 3. the deck stops; two idle polls later the replay resumes and uploads again.
	await page.locator('[data-testid="play-deck-1"]').click();
	await expect.poll(() => replayId(page), { timeout: 20_000 }).toMatch(/^[0-9a-f]{32}$/);
	await expect
		.poll(() => envelopes.slice(sentBeforeStop).some((e) => e.types.includes('replay_event')), {
			timeout: 30_000
		})
		.toBe(true);
});
