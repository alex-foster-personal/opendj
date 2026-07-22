import { expect, test } from '@playwright/test';

test('browser headphone device probe uses real enumerateDevices and setSinkId', async ({ page }) => {
	const probe = await page.evaluate(async () => {
		if (navigator.mediaDevices === undefined || typeof navigator.mediaDevices.enumerateDevices !== 'function') {
			return { ok: false, reason: 'navigator.mediaDevices.enumerateDevices is unavailable' };
		}
		if (typeof HTMLMediaElement.prototype.setSinkId !== 'function') {
			return { ok: false, reason: 'HTMLMediaElement.setSinkId is unavailable in this browser' };
		}
		const outputs = (await navigator.mediaDevices.enumerateDevices()).filter(
			(device) => device.kind === 'audiooutput'
		);
		if (outputs.length === 0) {
			return { ok: false, reason: 'browser exposed no audiooutput devices' };
		}
		const element = document.createElement('audio');
		try {
			await element.setSinkId(outputs[0].deviceId);
			return { ok: true, deviceId: outputs[0].deviceId };
		} catch (error) {
			return {
				ok: false,
				reason: `setSinkId rejected for enumerated output: ${error instanceof Error ? error.name + ': ' + error.message : String(error)}`
			};
		} finally {
			element.remove();
		}
	});

	test.skip(!probe.ok, `headphone device probe skipped: ${probe.reason ?? 'unknown browser/device reason'}`);
	expect(probe.ok).toBe(true);
});
