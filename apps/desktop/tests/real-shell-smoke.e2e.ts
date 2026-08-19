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
 *
 * WHAT THIS DRIVER CAN AND CANNOT SYNTHESIZE (measured here, Wed 19 Aug 2026).
 * A plain click reaches the app: the first row becomes the selection, and that
 * is asserted below precisely so a later failure cannot be confused with dead
 * input. A double-click does NOT reach the app's ondblclick, and moveTo() does
 * not produce a CSS :hover state, so the hover-revealed per-row load buttons
 * never display. Both gestures work in Playwright's webkit against the same
 * build, so these are limits of the embedded WebDriver in WKWebView, not
 * product defects. Gesture coverage therefore stays in tier 1 and the deck
 * load here goes through the agent-native IPC, which is the same load path.
 *
 * Acceptance tests:
 *
 * - [if] the Rust initialization_script stops injecting the origin [then ⛔️]
 *   test 1 passes (verified by mutation).
 * - [if] the bootstrap page stops navigating [then ⛔️] test 2 passes (verified).
 * - [if] the worklet loads from a blob: URL again, the defect that shipped
 *   [then ⛔️] test 3 passes (verified by mutation on a clean binary).
 * - [if] a click no longer reaches the app [then ⛔️] test 3 reports a load
 *   failure, it reports dead input instead.
 */
import { browser } from '@wdio/globals';

import { ENGINE_ORIGIN } from '../wdio.conf.js';

/** setup.js polls the engine every 3s, so allow more than one interval. */
const NAVIGATION_TIMEOUT_MS = 45_000;
const LOAD_TIMEOUT_MS = 60_000;

/** Mirrors tier 1: far below stretch-adapter's 15s timeout, far above a real
 * creation (tens of ms), so only the defect can trip it. */
const STRETCH_CREATE_CEILING_MS = 5_000;

const TRACK_ROW = '[data-testid="track-row"]';
const DECK_1_TITLE = 'section.rb-deck[data-deck="1"] .title';

/** The app's own persistence keys - see tests/e2e/webkit-deckload.spec.ts. */
const PREFS_STORAGE_KEY = 'mdt.rb.ui-prefs.v1';
const PREFS_VALUE = JSON.stringify({
	hide_broken_links: false,
	confirm: { dblclick_load_play: false }
});

/** Deck 1 as the app itself reports it, for a failure message worth reading. */
async function _deckDiagnostics(): Promise<string> {
	return JSON.stringify(
		await browser.execute(() => {
			const ipc = (
				window as {
					musicDjToolsPerformance?: {
						query: () => {
							decks: Record<string, { stable_id: string | null; command_error: string | null; processor_error: string | null }>;
						};
					};
					__mdtLastLoads?: unknown;
				}
			).musicDjToolsPerformance;
			const deck = ipc === undefined ? null : ipc.query().decks['1'];
			return {
				loadDispatched: deck === null ? 'IPC absent' : deck.stable_id !== null,
				commandError: deck?.command_error ?? null,
				processorError: deck?.processor_error ?? null,
				prefs: window.localStorage.getItem('mdt.rb.ui-prefs.v1'),
				dialogs: [...document.querySelectorAll('[role="dialog"], dialog, .modal')].map((el) =>
					(el as HTMLElement).innerText.slice(0, 160)
				),
				rows: document.querySelectorAll('[data-testid="track-row"]').length,
				firstRowClass: document.querySelector('[data-testid="track-row"]')?.className ?? null
			};
		})
	);
}

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

		// A plain click first, purely to prove clicks REACH the app in the shell:
		// the row responds by becoming the selection. Without it, a failure below
		// cannot be told apart from "the driver's input goes nowhere".
		await firstRow.click();
		await browser.waitUntil(
			async () => ((await firstRow.getAttribute('class')) ?? '').includes('rb-row-selected'),
			{
				timeout: LOAD_TIMEOUT_MS,
				timeoutMsg: 'a click on the first row did not select it: input is not reaching the app'
			}
		);

		// Load through the app's agent-native IPC, and here is the honest reason.
		// Two UI gestures reach this same load path, and NEITHER can be driven
		// through this driver in WKWebView. Measured, not assumed:
		//
		//   - double-click: no load is ever dispatched (stable_id null,
		//     command_error null, no dialog, prefs correct), while the identical
		//     double-click loads in Playwright's webkit against this same build.
		//   - the per-row "Load onto deck N" buttons: revealed by CSS
		//     `tbody tr:hover`, and moveTo() never produces that hover state, so
		//     the button stays undisplayed for the full timeout.
		//
		// Both are input-synthesis limits of the embedded WebDriver, not app
		// defects: the click above proves input reaches the app. The gesture
		// coverage therefore lives in tier 1, where gestures ARE synthesizable,
		// and this tier asserts what only the real shell can answer: that the
		// SHIPPED webview creates a worklet and decodes real audio.
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

		// The downstream effect: the deck leaves its empty state because a real
		// worklet was created and real audio decoded inside the shipped webview.
		// A bare "it never loaded" would leave the next reader guessing, so the
		// app's own load transaction is read back on failure.
		try {
			await browser.waitUntil(async () => (await deckTitle.getText()) !== 'No track loaded', {
				timeout: LOAD_TIMEOUT_MS
			});
		} catch {
			throw new Error(
				'deck 1 never left "No track loaded" in the real shell. This is the ' +
					'exact failure the packaged app shipped with, and the reason this tier ' +
					`exists. Deck 1 as the app reports it: ${await _deckDiagnostics()}`
			);
		}

		const bodyText = await browser.execute(() => document.body.innerText);
		expect(bodyText).not.toContain('StretchCommandTimeoutError');
		expect(bodyText).not.toContain('load failed');

		// The defect this tier exists for was a worklet that never constructed,
		// so the deck's own load transaction is the assertion that matters: a
		// stretchCreate stage that completed, and no processor error. A title
		// change alone could in principle survive a broken audio graph.
		const stages = await browser.execute(() => {
			const ipc = (
				window as {
					musicDjToolsPerformance?: {
						query: () => {
							decks: Record<
								string,
								{
									last_load_stages: { stretchCreate?: number } | null;
									processor_error: string | null;
									command_error: string | null;
								}
							>;
						};
					};
				}
			).musicDjToolsPerformance;
			if (ipc === undefined) throw new Error('performance IPC is not installed');
			const deck = ipc.query().decks['1'];
			return {
				stretchCreate: deck.last_load_stages?.stretchCreate ?? null,
				processorError: deck.processor_error,
				commandError: deck.command_error
			};
		});
		expect(stages.processorError).toBeNull();
		expect(stages.commandError).toBeNull();
		expect(stages.stretchCreate).not.toBeNull();
		// STRETCH_CREATE_TIMEOUT_MS is 15s; the fix measures tens of ms. A
		// ceiling far below the timeout can only be tripped by the defect.
		expect(stages.stretchCreate as number).toBeLessThan(STRETCH_CREATE_CEILING_MS);
	});

	it('opens the settings overlay on Cmd+, in the real WKWebView', async () => {
		// The one entry point tier 1 structurally cannot see: chromium fakes the
		// chord, only the shell answers whether OUR capture-phase listener and the
		// overlay render inside the shipped webview. Same driver-limit protocol as
		// the deck-load test above: a probe first measures whether keys() reaches
		// the page at all; if the embedded WebDriver cannot synthesize the chord
		// (like double-click and :hover, measured Wed 19 Aug 2026), the identical
		// KeyboardEvent is dispatched at the same window target, entering the same
		// capture listener in hotkeys.ts. Which delivery ran is asserted loudly,
		// never silently substituted.
		await browser.url(`${ENGINE_ORIGIN}/performance`);
		await browser.execute(() => {
			const w = window as { __mdtKeyProbe?: string[] };
			w.__mdtKeyProbe = [];
			window.addEventListener(
				'keydown',
				(e) => w.__mdtKeyProbe?.push(`${e.metaKey ? 'Meta+' : ''}${e.key}`),
				true
			);
		});

		await browser.keys(['Meta', ',', 'Meta']);
		const probeSaw = await browser.execute(
			() => (window as { __mdtKeyProbe?: string[] }).__mdtKeyProbe ?? []
		);
		const driverDelivers = probeSaw.some((k) => k === 'Meta+,');
		if (!driverDelivers) {
			// Measured driver limit, not an app defect: fall back to the same event
			// at the same target. Everything from the listener down is still real.
			await browser.execute(() => {
				window.dispatchEvent(
					new KeyboardEvent('keydown', { key: ',', metaKey: true, bubbles: true })
				);
			});
		}

		const dialog = await browser.$('[role="dialog"][aria-label="Settings"]');
		await dialog.waitForDisplayed({
			timeout: LOAD_TIMEOUT_MS,
			timeoutMsg:
				`Cmd+, never opened the settings overlay in the real shell ` +
				`(delivery: ${driverDelivers ? 'driver keys()' : 'window KeyboardEvent'}; ` +
				`probe saw: ${JSON.stringify(probeSaw)})`
		});

		// The setup entry point must be reachable in the COLLAPSED overlay: the
		// actions bar lives outside .so-body precisely so no search is needed.
		const runSetup = await dialog.$('button.so-action');
		await runSetup.waitForDisplayed({ timeout: LOAD_TIMEOUT_MS });
		expect(await runSetup.getText()).toBe('Run setup');

		// Escape closes, through whichever delivery the chord used.
		if (driverDelivers) {
			await browser.keys(['Escape']);
		} else {
			await browser.execute(() => {
				window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
			});
		}
		await browser.waitUntil(async () => !(await dialog.isDisplayed()), {
			timeout: LOAD_TIMEOUT_MS,
			timeoutMsg: 'Escape never closed the settings overlay in the real shell'
		});
	});
});
