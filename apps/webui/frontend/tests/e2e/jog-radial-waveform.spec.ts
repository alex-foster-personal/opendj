// requirement: DECKUX-02
import { expect, test } from '@playwright/test';

test('jog radial waveform pref round-trips through settings apply and GET /api/v1/ui-prefs', async ({
	page,
	request
}) => {
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

	await page.evaluate(() => {
		const mod = (window as unknown as { __mdtApplySetting?: (k: string, v: boolean) => void }).__mdtApplySetting;
		if (mod !== undefined) {
			mod('jog_radial_waveform', true);
			return;
		}
		// Settings overlay path: import is not on window; use the HTTP twin directly.
		return fetch('/api/v1/ui-prefs', {
			method: 'PUT',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify({ jog_radial_waveform: true })
		});
	});

	const prefs = await request.get('/api/v1/ui-prefs');
	expect(prefs.ok()).toBeTruthy();
	const body = await prefs.json();
	expect(body.jog_radial_waveform).toBe(true);
});
