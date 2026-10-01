// requirement: IOPIN-14 (the audio I/O panel never draws an unreadable device list as an empty one)
// [if] the browser names several outputs [then] the panel lists exactly those, offers no grant or retry, and the parity route returns the same list ⛔️
//
// Regression line: if a named listing loses, duplicates or renames a device between
// enumerateDevices() and the panel, the `listed` state is lying.
//
// Own file because `launchOptions` forces a new worker and may only be set at the top level.
import { expect, test, type Page } from '@playwright/test';

type Listed = { id: string; label: string };

async function headphones(page: Page): Promise<{ outputs: Listed[] }> {
	return page.evaluate(() => {
		const hp = window.musicDjToolsPerformance!.query().mixer.headphones as unknown as { outputs: Listed[] };
		return JSON.parse(JSON.stringify({ outputs: hp.outputs }));
	});
}

// Chromium's own fake capture devices, with its permission UI auto-accepted. Not this host's
// hardware, and labelled so: it is the only way to reach the `listed` state in a headless run,
// where a granted permission still returns placeholders and a real stream would raise a macOS
// prompt. What it proves is the path, on a real enumerateDevices(): several named devices in,
// the same several out, in the panel and on the parity route.
test.use({
	launchOptions: {
		args: [
			'--autoplay-policy=no-user-gesture-required',
			'--use-fake-device-for-media-stream',
			'--use-fake-ui-for-media-stream'
		]
	}
});

test('every named device is listed, with no action offered, and the parity route agrees', async ({ page }) => {
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	const raw = await page.evaluate(async () =>
		(await navigator.mediaDevices.enumerateDevices()).map((device) => ({
			kind: device.kind,
			deviceId: device.deviceId,
			label: device.label
		}))
	);
	const named = raw.filter((device) => device.kind === 'audiooutput' && device.label !== '');
	expect(named.length, 'control: the fake devices must be visible to the page').toBeGreaterThan(1);

	await page.getByRole('button', { name: 'SHOW AUDIO I/O' }).click();
	const panel = page.getByRole('dialog', { name: 'Audio I/O settings' });
	await expect(panel).toBeVisible();
	await expect(panel.locator('[data-io-device-access]')).toHaveAttribute('data-io-device-access', 'listed');
	await expect(panel.locator('[data-io-device-access-action]')).toHaveCount(0);
	await expect(panel.locator('[data-io-device-access-message]')).toHaveCount(0);
	const state = await headphones(page);
	expect(state.outputs).toEqual(named.map((device) => ({ id: device.deviceId, label: device.label })));
	expect(state.outputs.filter((output) => output.id === 'default')).toHaveLength(1);
	const optionLabels = await page
		.getByLabel('master output device', { exact: true })
		.locator('option:not([disabled])')
		.evaluateAll((options) => options.map((option) => (option.textContent ?? '').trim()));
	expect(optionLabels).toEqual(named.map((device) => device.label));

	// The route trails the panel by up to one mirror publish (a second), and
	// answers 503 before the first: poll for the list itself.
	await expect
		.poll(
			async () => {
				const response = await page.request.get('/api/v1/performance/headphones');
				if (response.status() !== 200) return `HTTP ${response.status()}`;
				const body = await response.json();
				return { status: body.device_access?.status, outputs: body.outputs };
			},
			{ timeout: 30_000 }
		)
		.toEqual({ status: 'listed', outputs: state.outputs });
});
