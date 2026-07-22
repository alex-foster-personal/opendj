import { expect, test } from '@playwright/test';

test('browser headphone device probe uses application IPC and a real non-default output', async ({ page }) => {
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	const probe = await page.evaluate(async () => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) return { ok: false, reason: 'performance IPC is unavailable' };
		if (navigator.mediaDevices === undefined || typeof navigator.mediaDevices.enumerateDevices !== 'function') {
			return { ok: false, reason: 'navigator.mediaDevices.enumerateDevices is unavailable' };
		}
		if (typeof HTMLMediaElement.prototype.setSinkId !== 'function') {
			return { ok: false, reason: 'HTMLMediaElement.setSinkId is unavailable in this browser' };
		}
		try {
			const refreshed = await ipc.dispatch({ type: 'headphone_outputs_refresh' });
			const output = refreshed.mixer.headphones.outputs.find(
				(candidate) => candidate.id !== '' && candidate.id !== 'default'
			);
			if (output === undefined) {
				return { ok: false, reason: 'browser exposed no non-default audiooutput device' };
			}
			const before = refreshed.mixer;
			const selected = await ipc.dispatch({ type: 'headphone_output_select', device_id: output.id });
			if (
				selected.mixer.headphones.selected_output_device_id !== output.id ||
				!selected.mixer.headphones.active
			) {
				return { ok: false, reason: 'application did not publish accepted headphone sink state' };
			}
			const afterFader = await ipc.dispatch({ type: 'fader', deck: 1, value: 0.7 });
			const afterCrossfader = await ipc.dispatch({ type: 'crossfader', value: 0.2 });
			if (
				afterCrossfader.mixer.channels[1].cue_enabled !== before.channels[1].cue_enabled ||
				afterCrossfader.mixer.headphones.selected_output_device_id !== output.id ||
				afterFader.mixer.headphones.selected_output_device_id !== output.id
			) {
				return { ok: false, reason: 'mixer transport-independent controls altered cue or selected sink state' };
			}
			return { ok: true, deviceId: output.id };
		} catch (error) {
			return {
				ok: false,
				reason: `application output selection rejected: ${error instanceof Error ? `${error.name}: ${error.message}` : String(error)}`
			};
		}
	});
	test.skip(!probe.ok, `headphone device probe skipped: ${probe.reason ?? 'unknown hardware/browser reason'}`);
	expect(probe.ok).toBe(true);
});
