import type { Page } from '@playwright/test';

// Imported for its `declare global` on window.musicDjToolsPerformance, which
// is what makes the predicate below type check against the real IPC.
import type { PerformanceBrowserIpc } from '../../../src/lib/rb/performance-ipc.svelte';

/**
 * Wait until the performance IPC is installed, which is when UI controls
 * start doing anything.
 *
 * /performance publishes `window.musicDjToolsPerformance` only after
 * `hydratePerformanceFeedback()` resolves (a network round trip), and that
 * install is what starts the command session. Until then every
 * `dispatchPerformanceCommand` throws "performance command session ...
 * was invalidated" and `runPerformanceCommandFromUi` swallows it: a key
 * press on the master volume or a click on the MIX knob is DROPPED, with no
 * toast and no error. A track row or a knob becomes visible before that on
 * a loaded runner, so a test that interacts as soon as its control renders
 * races hydration and fails as "the value did not change"
 * (tests/e2e/context-menu.spec.ts:106 failed that way in 6 of the 29 named
 * e2e failures sampled on Mon 21 Sep 2026; headphone-mix-click.spec.ts:59
 * likewise). 46 other specs already wait on this predicate; this is the
 * one place to spell it.
 */
export async function waitForPerformanceIpc(page: Page): Promise<void> {
	await page.waitForFunction(() => {
		// The annotation keeps the type import referenced (an unused import
		// would drop the `declare global` it carries) without exporting it
		// (an unused export is what the quality ratchet counts).
		const ipc: PerformanceBrowserIpc | undefined = window.musicDjToolsPerformance;
		return ipc?.version === 1;
	});
}
