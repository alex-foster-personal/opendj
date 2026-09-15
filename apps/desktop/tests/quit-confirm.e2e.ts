/**
 * INSTALL-21: Cmd-Q quit confirmation with a loaded, playing deck (tier 2).
 *
 * macOS-only. Linux CI carries unit + source scans instead.
 */
import { browser } from '@wdio/globals';

import { ENGINE_ORIGIN } from '../wdio.conf.js';
import {
	confirmQuitViaEnter,
	probeDriverDeliversMetaChords,
	requestQuitThroughShell,
	staleSessionSnapshot,
	waitForAppExit
} from './quit-confirm-helpers.js';

const LOAD_TIMEOUT_MS = 60_000;

const TRACK_ROW = '[data-testid="track-row"]';
const DECK_1_TITLE = 'section.rb-deck[data-deck="1"] .title';
const QUIT_DIALOG = '[data-testid="quit-confirm-dialog"]';

const PREFS_STORAGE_KEY = 'mdt.rb.ui-prefs.v1';
const PREFS_VALUE = JSON.stringify({
	hide_broken_links: false,
	confirm: { dblclick_load_play: false }
});

describe('Open DJ quit confirmation (playing deck)', () => {
	it('shows the dialog on Cmd-Q, keeps transport live, flushes on Enter, then exits', async () => {
		await browser.url(`${ENGINE_ORIGIN}/performance`);
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
			{ timeout: LOAD_TIMEOUT_MS }
		);

		const allTracks = await browser.$('[data-testid="playlist-tree"]').$('span=All Tracks');
		await allTracks.waitForDisplayed({ timeout: LOAD_TIMEOUT_MS });
		await allTracks.click();

		const firstRow = await browser.$(TRACK_ROW);
		await firstRow.waitForDisplayed({ timeout: LOAD_TIMEOUT_MS });
		const stableId = await firstRow.getAttribute('data-stable-id');
		if (stableId === null) throw new Error('first track row has no data-stable-id attribute');

		await browser.execute((id: string) => {
			const ipc = (
				window as {
					musicDjToolsPerformance?: {
						dispatch: (command: { type: string; deck: number; stable_id: string }) => void;
					};
				}
			).musicDjToolsPerformance;
			if (ipc === undefined) throw new Error('performance IPC is not installed');
			ipc.dispatch({ type: 'load', deck: 1, stable_id: id });
		}, stableId);

		const deckTitle = await browser.$(DECK_1_TITLE);
		await browser.waitUntil(async () => (await deckTitle.getText()) !== 'No track loaded', {
			timeout: LOAD_TIMEOUT_MS
		});

		await browser.execute(() => {
			const ipc = (
				window as {
					musicDjToolsPerformance?: {
						dispatch: (command: { type: string; deck: number }) => void;
					};
				}
			).musicDjToolsPerformance;
			if (ipc === undefined) throw new Error('performance IPC is not installed');
			ipc.dispatch({ type: 'play', deck: 1 });
		});

		await browser.waitUntil(
			async () =>
				await browser.execute(() => {
					const ipc = (
						window as {
							musicDjToolsPerformance?: {
								query: () => {
									decks: Record<string, { playing: boolean; audible: boolean }>;
								};
							};
						}
					).musicDjToolsPerformance;
					if (ipc === undefined) return false;
					const deck = ipc.query().decks['1'];
					return deck.playing || deck.audible;
				}),
			{ timeout: LOAD_TIMEOUT_MS }
		);

		const driverDelivers = await probeDriverDeliversMetaChords();
		await requestQuitThroughShell(driverDelivers);

		const dialog = await browser.$(QUIT_DIALOG);
		await dialog.waitForDisplayed({
			timeout: LOAD_TIMEOUT_MS,
			timeoutMsg:
				`Cmd-Q did not open the quit confirmation dialog in the real shell ` +
				`(delivery: ${driverDelivers ? 'driver keys()' : 'hook after synthetic chord'})`
		});

		const stillPlaying = await browser.execute(() => {
			const ipc = (
				window as {
					musicDjToolsPerformance?: {
						query: () => {
							decks: Record<string, { playing: boolean; audible: boolean }>;
						};
					};
				}
			).musicDjToolsPerformance;
			if (ipc === undefined) return false;
			const deck = ipc.query().decks['1'];
			return deck.playing || deck.audible;
		});
		expect(stillPlaying).toBe(true);

		const staleMs = await staleSessionSnapshot(120_000);
		expect(staleMs).not.toBeNull();

		const flushedMs = await confirmQuitViaEnter();
		expect(flushedMs).not.toBeNull();
		expect(flushedMs).not.toBe(staleMs);
		expect(Date.now() - (flushedMs as number)).toBeLessThan(5_000);

		await waitForAppExit(15_000);
	});
});
