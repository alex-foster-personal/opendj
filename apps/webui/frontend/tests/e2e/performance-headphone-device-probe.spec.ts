import { expect, test } from '@playwright/test';

test('browser headphone output acquisition starts from the visible I/O gesture', async ({ page }) => {
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
	// Accessible name is aria-label="SHOW AUDIO I/O" (AGENT-09 / CUEOUT-06), not the "I/O" glyph.
	const ioButton = page.getByRole('button', { name: 'SHOW AUDIO I/O' });
	await ioButton.click({ timeout: 5_000 });
	await expect(page.getByRole('dialog', { name: 'Audio I/O settings' })).toBeVisible();
	await page.getByRole('button', { name: 'Choose output / allow device access' }).click();
	await page.waitForFunction(() => {
		const state = window.musicDjToolsPerformance?.query();
		const headphones = state?.mixer.headphones;
		return (
			headphones?.active === true ||
			headphones?.error !== null ||
			state?.last_error !== null ||
			(headphones?.outputs.length ?? 0) > 0
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

	if (
		result?.mixer.headphones.outputs.length &&
		result.mixer.headphones.error === null &&
		result.last_error === null
	) {
		expect(result.mixer.headphones.supported).toBe(true);
		return;
	}

	const queryError = result?.mixer.headphones.error ?? result?.last_error;
	expect(queryError, 'hardware/API absence must be explicit in IPC query state').toMatch(
		/unsupported|unavailable|permission|denied|notallowed|timed out|no (audio )?output|getUserMedia/i
	);
	expect(await page.locator('.hp-error').textContent()).toContain(queryError);
	if (!browserSupport) expect(queryError).toMatch(/unsupported|unavailable|getUserMedia|permission|denied/i);
});
