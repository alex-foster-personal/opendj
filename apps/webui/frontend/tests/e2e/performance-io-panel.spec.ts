import { expect, test } from '@playwright/test';

test('opening I/O is read-only and does not acquire or change audio routes', async ({ page }) => {
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	const before = await page.evaluate(() => {
		const state = window.musicDjToolsPerformance!.query();
		return {
			history: state.history,
			mode: state.mixer.headphones.output_mode,
			master: state.mixer.headphones.selected_master_output_device_id,
			cue: state.mixer.headphones.selected_output_device_id,
			booth: state.mixer.booth.selected_output_device_id
		};
	});
	await page.getByRole('button', { name: 'SHOW AUDIO I/O' }).click();
	await expect(page.getByRole('dialog', { name: 'Audio I/O settings' })).toBeVisible();
	await page.waitForTimeout(250);
	const after = await page.evaluate(() => {
		const state = window.musicDjToolsPerformance!.query();
		return {
			history: state.history,
			mode: state.mixer.headphones.output_mode,
			master: state.mixer.headphones.selected_master_output_device_id,
			cue: state.mixer.headphones.selected_output_device_id,
			booth: state.mixer.booth.selected_output_device_id
		};
	});
	expect(after).toEqual(before);
	await page.getByRole('button', { name: 'Choose output / allow device access' }).click();
	await expect
		.poll(() =>
			page.evaluate(() =>
				window
					.musicDjToolsPerformance!.query()
					.history.some((command) => command.type === 'headphone_output_acquire')
			)
		)
		.toBe(true);
});

test('IOPIN-03: I/O hover waits 100 ms, uses compact copy, and hides on pointer leave', async ({
	page
}) => {
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	const trigger = page.getByRole('button', { name: 'SHOW AUDIO I/O' });
	const hint = page.getByRole('tooltip', { name: 'Audio I/O quick settings' });
	const compactCopy = 'Routes the room mix and your headphone cue to audio devices.';
	await page.mouse.move(0, 0);
	await trigger.hover();
	await expect(hint).toBeHidden();
	await page.waitForTimeout(100);
	await expect(hint).toBeVisible();
	await expect(hint).toContainText(compactCopy);
	await page.mouse.move(0, 0);
	await expect(hint).toBeHidden();
});

test('IOPIN-03: click opens the persistent panel with X, Esc hint, and WIP outputs above live pickers', async ({
	page
}) => {
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	const trigger = page.getByRole('button', { name: 'SHOW AUDIO I/O' });
	await trigger.click();
	const panel = page.getByRole('dialog', { name: 'Audio I/O settings' });
	await expect(panel).toBeVisible();
	await expect(panel.getByRole('button', { name: 'Close audio I/O settings' })).toBeVisible();
	await expect(panel.getByText('Esc or X to dismiss')).toBeVisible();
	await expect(panel.getByRole('button', { name: 'Practice output mode' })).toBeVisible();
	await expect(panel.getByRole('button', { name: 'Split cable output mode' })).toBeVisible();
	const future = panel.locator('[data-io-wip-output]');
	await expect(future).toHaveCount(3);
	await expect(future.first()).toContainText('BOOTH / MONITOR');
	await expect(future.first()).toContainText('WIP');
	const boothWipY = await future
		.first()
		.boundingBox()
		.then((b) => b?.y ?? 0);
	const masterY = await panel
		.getByLabel('master output device')
		.boundingBox()
		.then((b) => b?.y ?? 0);
	expect(boothWipY).toBeLessThan(masterY);
	await expect(future.getByRole('button', { name: 'Advanced channel assignment' })).toBeDisabled();
	await expect(panel.getByLabel('master output device')).toBeVisible();
	await expect(panel.getByLabel('headphone output device', { exact: true })).toBeVisible();
	await expect(panel.getByLabel('booth output device')).toHaveCount(0);
	await expect(panel.getByLabel('audio input device')).toBeVisible();
	await expect(
		page.locator('.hp').getByRole('button', { name: 'Practice output mode' })
	).toHaveCount(0);
	await page.keyboard.press('Escape');
	await expect(panel).toBeHidden();
	await trigger.click();
	await expect(panel).toBeVisible();
	await panel.getByRole('button', { name: 'Close audio I/O settings' }).click();
	await expect(panel).toBeHidden();
});

test('I/O mode and delay controls dispatch into the shared engine read model', async ({ page }) => {
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await page.getByRole('button', { name: 'SHOW AUDIO I/O' }).click();
	const panel = page.getByRole('dialog', { name: 'Audio I/O settings' });
	await panel.getByRole('button', { name: 'Split cable output mode' }).click();
	await expect
		.poll(() =>
			page.evaluate(() => window.musicDjToolsPerformance?.query().mixer.headphones.output_mode)
		)
		.toBe('split_cable');
	await panel.getByRole('button', { name: 'Two outputs output mode' }).click();
	await expect
		.poll(() =>
			page.evaluate(() => window.musicDjToolsPerformance?.query().mixer.headphones.output_mode)
		)
		.toBe('two_outputs');
	const delay = panel.getByRole('textbox', { name: 'head delay milliseconds' });
	await delay.fill('37');
	await expect
		.poll(() =>
			page.evaluate(() => window.musicDjToolsPerformance?.query().mixer.headphones.head_delay_ms)
		)
		.toBe(37);
	await delay.press('ArrowUp');
	await expect
		.poll(() =>
			page.evaluate(() => window.musicDjToolsPerformance?.query().mixer.headphones.head_delay_ms)
		)
		.toBe(38);
});

test('I/O keeps calibration and selectors visible together', async ({ page }) => {
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await page.getByRole('button', { name: 'SHOW AUDIO I/O' }).click();
	const panel = page.getByRole('dialog', { name: 'Audio I/O settings' });
	await panel.getByRole('button', { name: 'Two outputs output mode' }).click();
	await expect(panel.getByRole('button', { name: 'CALIBRATE CUE ALIGNMENT' })).toBeVisible();
	await expect(panel.getByLabel('master output device')).toBeVisible();
	await expect(panel.getByLabel('calibration stimulus')).toBeVisible();
	await expect(panel.locator('[data-io-calibration-block]')).toBeVisible();
});

test('I/O routing feedback stays inside the panel when it is closed', async ({ page }) => {
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await expect(page.locator('[data-io-routing-feedback]')).toHaveCount(0);
	await page.getByRole('button', { name: 'SHOW AUDIO I/O' }).click();
	const panel = page.getByRole('dialog', { name: 'Audio I/O settings' });
	await panel.getByRole('button', { name: 'Two outputs output mode' }).click();
	const routing = panel.locator('[data-io-routing-feedback]');
	await expect(routing).toHaveCount(1);
	await page.keyboard.press('Escape');
	await expect(page.locator('[data-io-routing-feedback]')).toHaveCount(0);
});

test('I/O rows expose signal meters on master cue and input', async ({ page }) => {
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await page.getByRole('button', { name: 'SHOW AUDIO I/O' }).click();
	const panel = page.getByRole('dialog', { name: 'Audio I/O settings' });
	for (const bus of ['master', 'cue', 'input']) {
		await expect(panel.locator(`[data-signal-bus="${bus}"][role="meter"]`)).toBeVisible();
	}
});

test('IOPIN-11323: compact hover hides on pointer leave and lists routing copy', async ({
	page
}) => {
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	const trigger = page.getByRole('button', { name: 'SHOW AUDIO I/O' });
	const hint = page.getByRole('tooltip', { name: 'Audio I/O quick settings' });
	await page.mouse.move(0, 0);
	await trigger.hover();
	await page.waitForTimeout(120);
	await expect(hint).toBeVisible();
	await expect(hint).toContainText('Routes the room mix and your headphone cue to audio devices.');
	await expect(hint).toContainText('Click I/O to set audio outputs');
	await page.mouse.move(0, 0);
	await expect(hint).toBeHidden();
});

test('IOPIN-11323: SET OUTPUTS alert clears after the first click this session', async ({
	page
}) => {
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await page.evaluate(() => sessionStorage.removeItem('mdt.io-set-outputs.clicked'));
	const unset = await page.evaluate(() => {
		const hp = window.musicDjToolsPerformance!.query().mixer.headphones;
		return hp.selected_master_output_device_id === null && hp.selected_output_device_id === null;
	});
	test.skip(!unset, 'fixture already chose MASTER or CUE');
	const trigger = page.getByRole('button', { name: 'SHOW AUDIO I/O' });
	await expect(trigger).toHaveAttribute('data-io-alert', 'unset');
	await trigger.click();
	await expect(page.getByRole('dialog', { name: 'Audio I/O settings' })).toBeVisible();
	await expect(trigger).not.toHaveAttribute('data-io-alert', 'unset');
});

test('IOPIN-11323: Rescan lives only inside the open Audio I/O panel', async ({ page }) => {
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await expect(page.locator('.hp').getByRole('button', { name: /Rescan/ })).toHaveCount(0);
	await page.getByRole('button', { name: 'SHOW AUDIO I/O' }).click();
	const panel = page.getByRole('dialog', { name: 'Audio I/O settings' });
	await expect(
		panel.getByRole('button', { name: /Rescan available headphone output devices/ })
	).toBeVisible();
});
