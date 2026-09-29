/**
 * CHROME-07 (issue #3886, Codex P1 4129637640 on PR #3896): the real
 * /performance flow from the bottom tray to the MIDI drawer.
 *
 * The tray MIDI chip opens the audio I/O view (ioSurface.open, which pins the
 * "Audio I/O" explainer dialog programmatically). The MIDI entry inside that
 * dialog must then open the MIDI drawer AND take the I/O dialog down: the I/O
 * popover sits at z-index 80 and the drawer at 41, so an I/O dialog left
 * pinned covers the drawer the operator just asked for.
 *
 * Lives in the root suite for the same reason as headphone-mix-click.spec.ts:
 * /performance only mounts behind a real backend with a fixture library, which
 * the root config builds and e2e.yml's "Root Playwright suite" step runs on
 * every PR. No skips: a missing control is a failure, not a pass.
 *
 * control-explainer-programmatic-pin.spec.ts stays as the component-level
 * complement: it pins the explainer's own edges (a parent unpin never reports
 * back, a user click pin survives) that this flow does not reach.
 *
 * Acceptance tests, "[if] <scenario> [then ⛔️]":
 * - [if] the tray MIDI chip does not open the Audio I/O dialog [then ⛔️]
 * - [if] MIDI inside the I/O dialog leaves the MIDI drawer closed [then ⛔️]
 * - [if] MIDI inside the I/O dialog leaves the I/O dialog up [then ⛔️]
 * - [if] the MIDI drawer's own trigger reports it collapsed while open [then ⛔️]
 */
// requirement: CHROME-07
import { expect, test } from '@playwright/test';

import { waitForPerformanceIpc } from './support/performance-ready';

test('tray MIDI opens audio I/O, and MIDI inside it swaps the I/O dialog for the MIDI drawer', async ({
	page
}) => {
	test.setTimeout(120_000);
	await page.setViewportSize({ width: 1440, height: 1000 });
	await page.goto('/performance');
	const trayMidi = page.getByRole('button', { name: 'Open audio I/O and MIDI' });
	await expect(trayMidi).toBeVisible({ timeout: 60_000 });
	await waitForPerformanceIpc(page);

	const ioDialog = page.getByRole('dialog', { name: 'Audio I/O' });
	const midiDrawer = page.getByRole('dialog', { name: 'MIDI devices and learn log' });
	await expect(midiDrawer).toHaveCount(0);

	await trayMidi.click();
	await expect(ioDialog).toBeVisible();

	const ioMidi = ioDialog.getByRole('button', { name: 'Open MIDI panel from audio I/O' });
	await expect(ioMidi).toBeVisible();
	await ioMidi.click();

	// The drawer module is lazy (MidiPanelLoader), so allow for its fetch.
	await expect(midiDrawer).toBeVisible({ timeout: 15_000 });
	await expect(ioDialog).toHaveCount(0);
	await expect(page.getByRole('button', { name: 'Open MIDI panel', exact: true })).toHaveAttribute(
		'aria-expanded',
		'true'
	);
});
