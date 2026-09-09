/**
 * Playlist detail browser coverage (#859).
 *
 * The production-artifact fixture seeds two real StateWriter playlists: one
 * with the generated fixture tracks and one empty. This opens both detail
 * routes and verifies the user-visible, honest "diff not computed" state.
 *
 * Acceptance tests:
 *
 * - [if] the fixture has a populated playlist [then ⛔️] its detail page does
 *   not render the playlist name and the explicit not-computed message.
 * - [if] the fixture has an empty playlist [then ⛔️] its detail page does not
 *   render the playlist name and the explicit not-computed message.
 */
import { expect, test } from '@playwright/test';

const NOT_COMPUTED_MESSAGE = 'Rekordbox/djay sync diff not computed for this playlist.';

test('playlist detail: populated and empty playlists render the honest diff state', async ({ page }) => {
	for (const playlist of [
		{ id: 'e2e-fixture-populated', name: 'E2E Fixture Set' },
		{ id: 'e2e-fixture-empty', name: 'E2E Empty Set' }
	]) {
		const response = await page.goto(`/playlist/${playlist.id}`);
		expect(response?.status(), `playlist route did not load for ${playlist.id}`).toBe(200);
		await expect(page.getByRole('heading', { name: playlist.name })).toBeVisible();
		await expect(page.getByText(NOT_COMPUTED_MESSAGE, { exact: true })).toBeVisible();
	}
});
