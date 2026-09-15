/**
 * INSTALL-21: immediate Cmd-Q quit with no loaded deck (tier 2).
 *
 * Separate spec so a prior confirmed-quit test does not share a dead shell session.
 */
import { browser } from '@wdio/globals';

import { ENGINE_ORIGIN } from '../wdio.conf.js';
import {
	probeDriverDeliversMetaChords,
	requestQuitThroughShell,
	waitForAppExit
} from './quit-confirm-helpers.js';

const NAVIGATION_TIMEOUT_MS = 45_000;
const QUIT_DIALOG = '[data-testid="quit-confirm-dialog"]';

describe('Open DJ quit confirmation (empty session)', () => {
	it('quits immediately through the shell when no deck is loaded', async () => {
		await browser.url(`${ENGINE_ORIGIN}/performance`);
		await browser.waitUntil(
			async () => (await browser.getUrl()).startsWith(ENGINE_ORIGIN),
			{ timeout: NAVIGATION_TIMEOUT_MS }
		);

		const driverDelivers = await probeDriverDeliversMetaChords();
		const dialog = await browser.$(QUIT_DIALOG);
		expect(await dialog.isExisting()).toBe(false);

		await requestQuitThroughShell(driverDelivers);

		await browser.waitUntil(
			async () => !(await dialog.isExisting()),
			{
				timeout: 1_000,
				timeoutMsg: 'quit confirmation dialog appeared with no deck loaded'
			}
		);

		await waitForAppExit(10_000);
	});
});
