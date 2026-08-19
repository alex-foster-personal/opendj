/**
 * TIER 2 SMOKE: the real Tauri window, the real WKWebView, one real deck load.
 *
 * Deliberately small. Tier 1 owns breadth (tempo, key, cue, loop, transport)
 * because it is fast, deterministic and needs no GUI session. This tier exists
 * only to cover what tier 1 structurally cannot see, so it asserts the three
 * things unique to the shell and then stops:
 *
 *   1. The Rust `initialization_script` really injected OPENDJ_ENGINE_ORIGIN.
 *   2. The bootstrap page really navigated the window off tauri://localhost
 *      onto the engine origin.
 *   3. The app, running in the ACTUAL shipped webview, can load a deck.
 *
 * Every assertion is a downstream effect, same rule as tier 1: no control is
 * asserted by reading back the value it just wrote.
 */
import { browser } from '@wdio/globals';

import { ENGINE_ORIGIN } from '../wdio.conf.js';

/** setup.js polls the engine every 3s, so allow more than one interval. */
const NAVIGATION_TIMEOUT_MS = 45_000;
const LOAD_TIMEOUT_MS = 60_000;

const TRACK_ROW = '[data-testid="track-row"]';
const DECK_1_TITLE = 'section.rb-deck[data-deck="1"] .title';

/** The app's own persistence keys - see tests/e2e/webkit-deckload.spec.ts. */
const PREFS_STORAGE_KEY = 'mdt.rb.ui-prefs.v1';
const PREFS_VALUE = JSON.stringify({
	hide_broken_links: false,
	confirm: { dblclick_load_play: false }
});

describe('Open DJ desktop shell', () => {
	it('injects the engine origin from Rust before any page script runs', async () => {
		const injected = await browser.execute(
			() => (globalThis as { OPENDJ_ENGINE_ORIGIN?: string }).OPENDJ_ENGINE_ORIGIN
		);
		// This value can only have come from main.rs's initialization_script:
		// nothing in the bundled bootstrap page sets it.
		expect(injected).toBe(ENGINE_ORIGIN);
	});

	it('navigates the shell window onto the engine origin', async () => {
		await browser.waitUntil(
			async () => (await browser.getUrl()).startsWith(ENGINE_ORIGIN),
			{
				timeout: NAVIGATION_TIMEOUT_MS,
				timeoutMsg:
					`the shell never left the bootstrap page. Last URL: ` +
					`${await browser.getUrl()}. That means the engine probe failed, ` +
					`which is the state the setup page is designed to explain.`
			}
		);
	});

	it('loads a track into deck 1 in the real WKWebView', async () => {
		await browser.url(`${ENGINE_ORIGIN}/performance`);

		// The app's REAL persistence, not a stub: the prefs blob it writes for
		// itself, so the double-click takes the deterministic no-confirm path.
		// Written on the engine origin, then reloaded so the app reads it at boot.
		await browser.execute(
			(key: string, value: string) => window.localStorage.setItem(key, value),
			PREFS_STORAGE_KEY,
			PREFS_VALUE
		);
		await browser.url(`${ENGINE_ORIGIN}/performance`);
		await browser.waitUntil(
			async () =>
				await browser.execute(
					() =>
						(window as { musicDjToolsPerformance?: { version: number } })
							.musicDjToolsPerformance?.version === 1
				),
			{
				timeout: LOAD_TIMEOUT_MS,
				timeoutMsg: 'the performance IPC never installed in the real shell webview'
			}
		);

		// The browser opens on a blank pane; All Tracks is what fills the table.
		const allTracks = await browser.$('[data-testid="playlist-tree"]').$('span=All Tracks');
		await allTracks.waitForDisplayed({ timeout: LOAD_TIMEOUT_MS });
		await allTracks.click();

		const firstRow = await browser.$(TRACK_ROW);
		await firstRow.waitForDisplayed({ timeout: LOAD_TIMEOUT_MS });

		const deckTitle = await browser.$(DECK_1_TITLE);
		await deckTitle.waitForDisplayed({ timeout: LOAD_TIMEOUT_MS });
		expect(await deckTitle.getText()).toBe('No track loaded');

		await (await firstRow.$('td.c-title')).doubleClick();

		// The downstream effect: the deck leaves its empty state because a real
		// worklet was created and real audio decoded inside the shipped webview.
		await browser.waitUntil(async () => (await deckTitle.getText()) !== 'No track loaded', {
			timeout: LOAD_TIMEOUT_MS,
			timeoutMsg:
				'deck 1 never left "No track loaded" in the real shell. This is the ' +
				'exact failure the packaged app shipped with, and the reason this ' +
				'tier exists.'
		});

		const bodyText = await browser.execute(() => document.body.innerText);
		expect(bodyText).not.toContain('StretchCommandTimeoutError');
		expect(bodyText).not.toContain('load failed');
	});
});
