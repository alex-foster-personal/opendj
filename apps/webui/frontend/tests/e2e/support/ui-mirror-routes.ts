import type { Page } from '@playwright/test';

/**
 * Hermetic stubs for the agent ui-mirror and its tab-leadership lease (AGENT-18).
 *
 * `leased-mirror-publisher.ts` reads `GET /api/v1/state/ui-mirror/lease` before a
 * leader's first PUT and on every follower recheck (#5471, #5482, #5505). A spec
 * that stubs only the exact mirror path sees that lease read fall through to its
 * catch-all, which is how main's E2E at 1157979ff failed `unexpectedRequests`.
 * The lease answers "free", so the boot tab claims it and PUTs into the stubbed
 * mirror, exactly as a single-tab packaged app does.
 */
export async function stubUiMirrorRoutes(page: Page): Promise<void> {
	await page.route('**/api/v1/state/ui-mirror', (route) => route.fulfill({ json: {} }));
	await page.route('**/api/v1/state/ui-mirror/lease', (route) =>
		route.fulfill({ json: { held: false, holder: null, holder_playing: null, holder_yieldable: null } })
	);
}
