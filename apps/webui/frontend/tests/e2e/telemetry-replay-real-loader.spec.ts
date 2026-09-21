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
 *   3. after the deck stops, the poll restarts the replay and uploads resume;
 *   4. a segment recorded while a fixture track title is on screen, decoded
 *      from the envelope (pako-deflated rrweb events), does not contain it:
 *      text masking plus the explicit attribute mask list hold on the real
 *      recorder, not just in the options object.
 *
 * UNAVAILABLE, not a fake pass, whenever the engine has no live client or no
 * frontend DSN (every CI run today): the consent route then withholds the
 * loader URL. To run it for real, export OPENDJ_TELEMETRY=1, SENTRY_DSN and
 * SENTRY_FRONTEND_DSN for the engine before `pnpm test:e2e`, on a host that
 * can reach js-*.sentry-cdn.com. It sends one short replay to the fe project.
 */
import { inflateSync } from 'node:zlib';

import { expect, test, type Page } from '@playwright/test';

const INGEST = /ingest\.(?:[a-z]+\.)?sentry\.io\/api\/\d+\/envelope/;
const FLUSH_INTERVAL_MS = 5_500; // Sentry replay flushMaxDelay default
const TRACK_ROW = '[data-testid="track-row"]';

/**
 * Walk a Sentry envelope: header line, then (item header, payload) pairs where
 * a `length` header means that many raw bytes follow. Replay recordings are a
 * one-line JSON header plus pako-deflated rrweb events; inflate them so the
 * recorded DOM can be read. Returns the item types and every decoded
 * recording as text.
 */
function parseEnvelope(raw: Buffer): { types: string[]; recordings: string[] } {
	const types: string[] = [];
	const recordings: string[] = [];
	let offset = raw.indexOf(0x0a) + 1; // past the envelope header line
	while (offset > 0 && offset < raw.length) {
		const nl = raw.indexOf(0x0a, offset);
		if (nl < 0) break;
		let header: { type?: string; length?: number };
		try {
			header = JSON.parse(raw.subarray(offset, nl).toString('utf8')) as typeof header;
		} catch {
			break;
		}
		offset = nl + 1;
		let payload: Buffer;
		if (typeof header.length === 'number') {
			payload = raw.subarray(offset, offset + header.length);
			offset += header.length + 1;
		} else {
			const end = raw.indexOf(0x0a, offset);
			payload = raw.subarray(offset, end < 0 ? raw.length : end);
			offset = end < 0 ? raw.length : end + 1;
		}
		if (typeof header.type === 'string') types.push(header.type);
		if (header.type === 'replay_recording') {
			const split = payload.indexOf(0x0a);
			const events = split < 0 ? payload : payload.subarray(split + 1);
			try {
				recordings.push(inflateSync(events).toString('utf8'));
			} catch {
				recordings.push(events.toString('utf8'));
			}
		}
	}
	return { types, recordings };
}

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
	const envelopes: Array<{ at: number; types: string[]; recordings: string[] }> = [];
	page.on('request', (r) => {
		if (r.method() !== 'POST' || !INGEST.test(r.url())) return;
		const raw = r.postDataBuffer();
		if (raw === null) {
			envelopes.push({ at: Date.now(), types: ['(no body)'], recordings: [] });
			return;
		}
		const { types, recordings } = parseEnvelope(raw);
		envelopes.push({ at: Date.now(), types, recordings });
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
	// The recording must not carry what the screen shows: the fixture title is
	// visible in the track table (text AND `title` attributes) and on the deck,
	// so wait for a segment recorded with it on screen and read the decoded
	// rrweb events for it. Positive control first: the DOM really has it.
	// The cell's `title` attribute is the bare track title; its innerText also
	// carries the hover-only deck chooser ("load to deck: 1 2 3 4").
	const fixtureTitle = ((await row.locator('td.c-title').getAttribute('title')) ?? '').trim();
	expect(fixtureTitle.length).toBeGreaterThan(0);
	await expect(page.getByText(fixtureTitle).first()).toBeVisible();
	const recordedBefore = envelopes.reduce((n, e) => n + e.recordings.length, 0);
	await expect
		.poll(() => envelopes.reduce((n, e) => n + e.recordings.length, 0), { timeout: 30_000 })
		.toBeGreaterThan(recordedBefore);
	const decoded = envelopes.flatMap((e) => e.recordings);
	expect(decoded.length, 'at least one recording was decodable').toBeGreaterThan(0);
	expect(decoded.some((text) => text.includes('rrweb') || text.includes('"type":'))).toBe(true);
	for (const text of decoded) {
		expect(text, 'a recorded segment carries the visible track title').not.toContain(fixtureTitle);
	}
	// Presence, not just absence: the click on the title cell above is a
	// `ui.click` breadcrumb the real SDK built from the live element, so its
	// selector carried `[title="<track>"]` into `beforeAddRecordingEvent`.
	// Seeing the filtered marker in a decoded segment proves the hook ran
	// inside the loader SDK, not in a unit-test stand-in.
	await expect
		.poll(
			() =>
				envelopes
					.flatMap((e) => e.recordings)
					.some((text) => text.includes('ui.click') && text.includes('[title=\\"[filtered]\\"]')),
			{ timeout: 30_000 }
		)
		.toBe(true);
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
