import { expect, test, type Locator, type Page } from '@playwright/test';
import type { APIRequestContext } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { fixtureManifestExists, readFixtureManifest } from './support/fixture-manifest';
import {
	ensureMoreDeckLayout,
	visibleDeckOne,
	visibleMixerChannelOne
} from './support/performance-visible-layout';

const MANIFEST_PATH = fileURLToPath(
	new URL('./fixtures/performance-playwright-data/fixture-manifest.json', import.meta.url)
);
// The config's own switch. A manifest left on disk by an earlier fixture run must not
// re-enable these cases when PERFORMANCE_E2E_FIXTURE=0 points the suite at a real library.
const FIXTURE_MODE_READY =
	process.env.PERFORMANCE_E2E_FIXTURE !== '0' && fixtureManifestExists(MANIFEST_PATH);

// The performance config can attach to a caller's backend or copy a real
// library's prefs, so each test sets deck_hover_link itself and puts back
// whatever value it found.
async function readLink(request: APIRequestContext): Promise<boolean> {
	const res = await request.get('/api/v1/ui-prefs');
	expect(res.ok()).toBe(true);
	const value = (await res.json()).deck_hover_link;
	expect(typeof value).toBe('boolean');
	return value;
}
async function setLink(request: APIRequestContext, on: boolean): Promise<void> {
	const res = await request.put('/api/v1/ui-prefs', {
		data: { deck_hover_link: on }
	});
	expect(res.ok()).toBe(true);
	expect((await res.json()).deck_hover_link).toBe(on);
}

// IOPIN-02, DECKUX-5917; Source: comment-pin 990d39d59328. Real page/pointer/computed styles.
// The link is one static 3px accent tick per panel: left edge on the deck, top
// edge on the mixer column. No ring, and the panel fill does not change.
test('deck and mixer show the same accent link tick and nothing else', async ({
	page,
	request
}) => {
	const original = await readLink(request);
	try {
		await setLink(request, true);
		await page.goto('/performance');
		await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
		await ensureMoreDeckLayout(page);
		const deck = visibleDeckOne(page);
		const channel = visibleMixerChannelOne(page);
		// The top-left corner sits on the top bar, outside every deck-linked panel.
		const away = { hover: () => page.mouse.move(1, 1) };
		const chrome = (el: Element) => {
			const s = getComputedStyle(el);
			const probe = document.createElement('span');
			probe.style.color = 'var(--rb-accent)';
			el.appendChild(probe);
			const accent = getComputedStyle(probe).color;
			probe.remove();
			return {
				accent,
				boxShadow: s.boxShadow,
				outline: s.outlineStyle,
				background: s.backgroundColor
			};
		};
		const read = (loc: Locator) => loc.evaluate(chrome);

		await away.hover();
		await expect(deck).not.toHaveClass(/deck-focus/);
		const deckRest = await read(deck);
		const channelRest = await read(channel);
		expect(deckRest.boxShadow).not.toContain('3px 0px 0px 0px inset');

		await channel.hover();
		await expect(deck).toHaveClass(/deck-focus/);
		await expect(channel).toHaveClass(/deck-focus/);
		const deckOn = await read(deck);
		const channelOn = await read(channel);
		expect(deckOn.accent).toMatch(/^rgba?\(/);
		expect(deckOn.boxShadow.startsWith(`${deckOn.accent} 3px 0px 0px 0px inset`)).toBe(true);
		expect(channelOn.boxShadow.startsWith(`${channelOn.accent} 0px 3px 0px 0px inset`)).toBe(true);
		expect(channelOn.accent).toBe(deckOn.accent);
		// Control for the overshoot: the old ring and fill must not come back.
		expect(deckOn.outline).toBe('none');
		expect(channelOn.outline).toBe('none');
		expect(deckOn.background).toBe(deckRest.background);
		expect(channelOn.background).toBe(channelRest.background);

		await deck.hover();
		await expect(channel).toHaveClass(/deck-focus/);
		await away.hover();
		await expect(deck).not.toHaveClass(/deck-focus/);
		await expect(channel).not.toHaveClass(/deck-focus/);
	} finally {
		await setLink(request, original);
	}
});

// DECKUX-5917b: with the "Deck hover link highlight" setting off (set through the
// production PUT /api/v1/ui-prefs twin and hydrated by the real page), hovering
// a mixer channel lights no linked panel. Control: back on, the same hover
// lights the deck again, so the off case is not passing on a hover that never
// landed.
test('turning the deck hover link setting off stops every linked tick', async ({
	page,
	request
}) => {
	test.skip(!FIXTURE_MODE_READY, 'needs fixture mode and the generated fixture library');
	const original = await readLink(request);
	const deck = visibleDeckOne(page);
	const channel = visibleMixerChannelOne(page);
	const wave = page.locator('.rb-waverow[data-deck="1"]');
	const shadow = (loc: Locator) => loc.evaluate((el) => getComputedStyle(el).boxShadow);
	try {
		await setLink(request, false);
		await page.goto('/performance');
		await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
		await ensureMoreDeckLayout(page);
		const linked = await loadNonMasterDeck(page);
		const row = page.locator(`tr[data-testid="track-row"][data-stable-id="${linked}"]`);

		// Off: hovering the channel lights no linked panel. Each panel is compared with its own
		// resting shadow, because a loaded deck already paints its own inset edge at rest.
		await page.mouse.move(1, 1);
		await expect(row).toHaveClass(/\bloaded\b/);
		const restDeck = await shadow(deck);
		const restWave = await shadow(wave);
		const restRow = await shadow(row);
		await channel.hover();
		await expect(deck).not.toHaveClass(/deck-focus/);
		await expect(channel).not.toHaveClass(/deck-focus/);
		await expect(wave).not.toHaveClass(/deck-focus/);
		await expect(row).not.toHaveClass(/rb-row-deck-hover/);
		expect(await shadow(deck)).toBe(restDeck);
		expect(await shadow(wave)).toBe(restWave);
		expect(await shadow(row)).toBe(restRow);

		// Control: back on, the same hover lights all three panels, so the off case
		// is not passing on a hover that never landed.
		await setLink(request, true);
		await page.reload();
		await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
		await loadNonMasterDeck(page);
		await page.mouse.move(1, 1);
		await channel.hover();
		await expect(deck).toHaveClass(/deck-focus/);
		await expect(wave).toHaveClass(/deck-focus/);
		await expect(row).toHaveClass(/rb-row-deck-hover/);
		const accent = await accentPrefix(deck);
		expect((await shadow(deck)).startsWith(`${accent} 3px 0px 0px 0px inset`)).toBe(true);
		expect((await shadow(wave)).startsWith(`${accent} 3px 0px 0px 0px inset`)).toBe(true);
		expect((await shadow(row)).startsWith(`${accent} 3px 0px 0px 0px inset`)).toBe(true);
	} finally {
		await cleanup([
			() => setLink(request, original),
			() => dispatch(page, { type: 'unload', deck: 1 }),
			() => dispatch(page, { type: 'unload', deck: 2 })
		]);
	}
});

/**
 * Runs every cleanup step, then rethrows the first failure. A step that rejects
 * never skips the ones after it, so the preference is restored and both decks
 * are unloaded whatever failed earlier.
 */
async function cleanup(steps: Array<() => Promise<unknown>>): Promise<void> {
	const failures: unknown[] = [];
	for (const step of steps) {
		try {
			await step();
		} catch (error) {
			failures.push(error);
		}
	}
	if (failures.length > 0) throw failures[0];
}

async function dispatch(page: Page, command: Record<string, unknown>): Promise<void> {
	await page.evaluate((command) => {
		if (window.musicDjToolsPerformance === undefined)
			throw new Error('performance IPC unavailable');
		return window.musicDjToolsPerformance.dispatch(command as never);
	}, command);
}

// Shared setup for the row cases: deck 1 holds a non-master track and deck 2 is
// the master, so deck 1's library row is loaded and not master. Hovering deck 1
// must give that row the same accent tick the deck shows.
async function loadNonMasterDeck(page: Page): Promise<string> {
	const { tracks } = readFixtureManifest(MANIFEST_PATH);
	const [linked, master] = [tracks[0]!.stable_id, tracks[1]!.stable_id];
	await dispatch(page, { type: 'load', deck: 1, stable_id: linked });
	await dispatch(page, { type: 'load', deck: 2, stable_id: master });
	await dispatch(page, { type: 'master', deck: 2 });
	return linked;
}

/** The accent prefix of the deck's own tick, read off the real deck element while it is hovered. */
async function accentPrefix(deck: Locator): Promise<string> {
	const shadow = await deck.evaluate((el) => getComputedStyle(el).boxShadow);
	const at = shadow.indexOf(' 3px 0px 0px 0px inset');
	expect(at, 'deck shows the accent tick').toBeGreaterThan(0);
	return shadow.slice(0, at);
}

// DECKUX-5917 acceptance for the two row states the tick must survive: a loaded
// row (the `loaded` edge rule has more specificity than the plain tick rule) and
// an analysis-job row (`rb-row-job` zeroes box-shadow with !important). Each case
// also reads the row with the hover gone, so the accent is proven to come from
// the link and not from the row's own styling.
test.describe('library row carries the link tick on a loaded and a job row', () => {
	test.skip(!FIXTURE_MODE_READY, 'needs fixture mode and the generated fixture library');

	test('a loaded, non-master library row keeps the accent tick under the hovered deck', async ({
		page,
		request
	}) => {
		const original = await readLink(request);
		try {
			await setLink(request, true);
			await page.goto('/performance');
			await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
			await ensureMoreDeckLayout(page);
			const linked = await loadNonMasterDeck(page);
			const row = page.locator(`tr[data-testid="track-row"][data-stable-id="${linked}"]`);
			const deck = visibleDeckOne(page);
			const shadow = (loc: Locator) => loc.evaluate((el) => getComputedStyle(el).boxShadow);

			await page.mouse.move(1, 1);
			await expect(row).toHaveClass(/\bloaded\b/);
			await expect(row).not.toHaveClass(/rb-row-master/);
			const rest = await shadow(row);

			await deck.hover();
			await expect(row).toHaveClass(/rb-row-deck-hover/);
			const accent = await accentPrefix(deck);
			const lit = await shadow(row);
			expect(lit.startsWith(`${accent} 3px 0px 0px 0px inset`)).toBe(true);
			// Control: at rest the same loaded row shows its yellow edge, not the accent.
			expect(rest.startsWith(`${accent} 3px 0px 0px 0px inset`)).toBe(false);
		} finally {
			await cleanup([
				() => setLink(request, original),
				() => dispatch(page, { type: 'unload', deck: 1 }),
				() => dispatch(page, { type: 'unload', deck: 2 })
			]);
		}
	});

	test('an analysis-job row on the hovered deck keeps the accent tick', async ({
		page,
		request
	}) => {
		const original = await readLink(request);
		try {
			await setLink(request, true);
			await page.goto('/performance?simulateJob=1');
			await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
			await ensureMoreDeckLayout(page);
			const linked = await loadNonMasterDeck(page);
			// The real job store path (the QA simulate hook, allowed on ?simulateJob=1).
			// A long run keeps the row in job state for the whole assertion.
			await page.evaluate(
				(id) =>
					(
						window as unknown as {
							__jobProgressSimulate: (kind: string, stableId: string, ms: number) => void;
						}
					).__jobProgressSimulate('vocals', id, 60_000),
				linked
			);
			const row = page.locator(`tr[data-testid="track-row"][data-stable-id="${linked}"]`);
			const deck = visibleDeckOne(page);
			const shadow = (loc: Locator) => loc.evaluate((el) => getComputedStyle(el).boxShadow);

			await page.mouse.move(1, 1);
			await expect(row).toHaveClass(/rb-row-job/);
			const rest = await shadow(row);

			await deck.hover();
			await expect(row).toHaveClass(/rb-row-deck-hover/);
			await expect(row).toHaveClass(/rb-row-job/);
			const accent = await accentPrefix(deck);
			const lit = await shadow(row);
			expect(lit.startsWith(`${accent} 3px 0px 0px 0px inset`)).toBe(true);
			// Control: the job row without the hover has no accent tick.
			expect(rest.startsWith(`${accent} 3px 0px 0px 0px inset`)).toBe(false);
		} finally {
			await cleanup([
				() => setLink(request, original),
				() => dispatch(page, { type: 'unload', deck: 1 }),
				() => dispatch(page, { type: 'unload', deck: 2 })
			]);
		}
	});
	// DECKUX-5917: every linked library row gets the tick, the master row included.
	// The master's gold edge stays underneath it, unchanged. Control: the unhovered
	// master row has the gold edge and no accent tick.
	test('a master-deck library row gets the accent tick over its unchanged master edge', async ({
		page,
		request
	}) => {
		const original = await readLink(request);
		try {
			await setLink(request, true);
			await page.goto('/performance');
			await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
			await ensureMoreDeckLayout(page);
			const linked = await loadNonMasterDeck(page);
			await dispatch(page, { type: 'master', deck: 1 });
			const row = page.locator(`tr[data-testid="track-row"][data-stable-id="${linked}"]`);
			const deck = visibleDeckOne(page);
			const shadow = (loc: Locator) => loc.evaluate((el) => getComputedStyle(el).boxShadow);

			await page.mouse.move(1, 1);
			await expect(row).toHaveClass(/rb-row-master/);
			const rest = await shadow(row);

			await deck.hover();
			const accent = await accentPrefix(deck);
			await expect(row).toHaveClass(/rb-row-master/);
			await expect(row).toHaveClass(/rb-row-deck-hover/);
			const lit = await shadow(row);
			expect(lit.startsWith(`${accent} 3px 0px 0px 0px inset`)).toBe(true);
			// The master edge is the layer under the tick, not replaced by it.
			expect(lit.endsWith(rest)).toBe(true);
			expect(rest.startsWith(`${accent} 3px 0px 0px 0px inset`)).toBe(false);
		} finally {
			await cleanup([
				() => setLink(request, original),
				() => dispatch(page, { type: 'unload', deck: 1 }),
				() => dispatch(page, { type: 'unload', deck: 2 })
			]);
		}
	});
});
