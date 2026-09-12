import { expect, test } from '@playwright/test';

test('browser headphone output acquisition starts from the visible + OUT gesture', async ({ page }) => {
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	// Quiet headed runs; gain > 0 so presentation/audible checks stay valid.
	await page.evaluate(async () => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		await ipc.dispatch({ type: 'master_volume', value: 0.1 });
	});

	const browserSupport = await page.evaluate(
		() =>
			navigator.mediaDevices !== undefined &&
			typeof (navigator.mediaDevices as MediaDevices & { selectAudioOutput?: unknown })
				.selectAudioOutput === 'function' &&
			typeof HTMLMediaElement.prototype.setSinkId === 'function'
	);
	// Accessible name is aria-label="ADD OUTPUT" (AGENT-09), not the "+ OUT" glyph.
	const addOutput = page.getByRole('button', { name: 'ADD OUTPUT' });
	await addOutput.click({ timeout: 5_000 });
	await page.waitForFunction(() => {
		const state = window.musicDjToolsPerformance?.query();
		return (
			state?.mixer.headphones.active === true ||
			state?.mixer.headphones.error !== null ||
			state?.last_error !== null
		);
	});

	const result = await page.evaluate(() => window.musicDjToolsPerformance?.query());
	if (result?.mixer.headphones.active === true) {
		expect(result.mixer.headphones.selected_output_device_id).not.toBeNull();
		expect(
			result.mixer.headphones.outputs.some(
				(output) => output.id === result.mixer.headphones.selected_output_device_id
			)
		).toBe(true);
		return;
	}

	const queryError = result?.mixer.headphones.error ?? result?.last_error;
	expect(queryError, 'hardware/API absence must be explicit in IPC query state').toMatch(
		/unsupported|unavailable|permission|denied|notallowed|timed out|no (audio )?output/i
	);
	expect(await page.locator('.hp-error').textContent()).toContain(queryError);
	if (!browserSupport) expect(queryError).toMatch(/unsupported|unavailable/i);
});
