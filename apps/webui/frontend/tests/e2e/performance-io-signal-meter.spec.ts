import { expect, test } from '@playwright/test';

// IOPIN-08: real decoded audio + production graph, not physical audibility proof.
test('I/O meters observe real audio and CUE volume rather than invented activity', async ({ page }) => {
	const track = process.env.PERFORMANCE_E2E_MIXTOUR_TRACK;
	if (!track) throw new Error('Set PERFORMANCE_E2E_MIXTOUR_TRACK to a real analyzed track');
	await page.goto('/performance?muted=1');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await page.evaluate(async (stable_id) => {
		const ipc = window.musicDjToolsPerformance!;
		await ipc.dispatch({ type: 'master_volume', value: 0.1 });
		await ipc.dispatch({ type: 'load', deck: 1, stable_id });
		await ipc.dispatch({ type: 'headphone_outputs_refresh' });
		const cueOutput = ipc.query().mixer.headphones.outputs.find((output) => output.id.trim() !== '');
		if (cueOutput === undefined) {
			throw new Error('UNAVAILABLE: browser exposed no selectable audio output for the real CUE monitor');
		}
		await ipc.dispatch({ type: 'headphone_output_select', device_id: cueOutput.id });
		if (!ipc.query().mixer.headphones.active) {
			throw new Error('UNAVAILABLE: browser did not activate the selected real CUE output');
		}
		await ipc.dispatch({ type: 'channel_cue', deck: 1, enabled: true });
		await ipc.dispatch({ type: 'headphone_mix', value: 0 });
		await ipc.dispatch({ type: 'headphone_level', value: 0 });
		await ipc.dispatch({ type: 'play', deck: 1, playing: true });
	}, track);
	const signals = () => page.evaluate(() => window.musicDjToolsPerformance!.query().mixer.headphones.signals);
	await expect.poll(async () => (await signals()).master.rms ?? 0).toBeGreaterThan(0.0001);
	await expect.poll(async () => (await signals()).cue.rms).toBe(0);
	await page.evaluate(() => window.musicDjToolsPerformance!.dispatch({ type: 'headphone_level', value: 0.5 }));
	await expect.poll(async () => (await signals()).cue.rms ?? 0).toBeGreaterThan(0.0001);
	const measured = await signals();
	expect(measured.master.physical_output_proven).toBe(false);
	expect(measured.cue.physical_output_proven).toBe(false);
	expect(measured.input.state).toBe('inactive');
	await page.evaluate(async () => {
		const ipc = window.musicDjToolsPerformance!;
		await ipc.dispatch({ type: 'play', deck: 1, playing: false });
		await ipc.dispatch({ type: 'unload', deck: 1 });
	});
});
