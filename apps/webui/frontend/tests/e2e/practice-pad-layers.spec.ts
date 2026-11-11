/** SKIN-10467, issue #12072: real panel endpoint and map, with no intercepted responses. */
import { expect, test } from '@playwright/test';

for (const mode of ['beat_jump', 'slip_loop']) {
	for (const ending of ['pointerup', 'pointercancel', 'lostpointercapture'] as const) {
		test(`held HOT CUE releases once after selecting ${mode} via ${ending}`, async ({ page }) => {
			const dispatches: string[] = [];
			page.on('console', (message) => {
				if (message.text().startsWith('[practice] d1-pad-1 ')) dispatches.push(message.text());
			});
			await page.goto('/practice/xdj-az');
			const pad = page.locator('[data-control-id="d1-pad-1"]');
			const button = pad.locator('button');
			await button.evaluate((element) => {
				element.addEventListener(
					'pointerdown',
					(event) => {
						element.dataset.testPointerId = String((event as PointerEvent).pointerId);
					},
					{ once: true }
				);
			});
			await button.hover();
			await page.mouse.down();
			await expect.poll(() => dispatches.length).toBe(1);
			const pointerId = await button.evaluate((element) => {
				const id = Number(element.dataset.testPointerId);
				if (!element.hasPointerCapture(id)) throw new Error('pad did not capture mouse');
				return id;
			});
			await page.evaluate((selectedMode) => {
				const ipc = window.musicDjToolsPracticeLayers;
				if (!ipc) throw new Error('missing inspection IPC');
				ipc.dispatch({ type: 'select', group: 'd1', mode: selectedMode });
			}, mode);
			await expect(pad).toHaveAttribute('data-status', mode === 'beat_jump' ? 'inert' : 'unwired');
			// The original capture target must survive the bound-to-unavailable DOM transition.
			await expect(button).toHaveCount(1);
			expect(await button.evaluate((element, id) => element.hasPointerCapture(id), pointerId)).toBe(
				true
			);
			if (ending === 'pointercancel') await button.dispatchEvent('pointercancel', { pointerId });
			else if (ending === 'lostpointercapture')
				await button.evaluate((element, id) => element.releasePointerCapture(id), pointerId);
			await page.mouse.up();
			await expect.poll(() => dispatches.length).toBe(2);
			expect(dispatches[0]).toContain('ch 6 note 0');
			expect(dispatches[0]).toContain('"pressed":true');
			expect(dispatches[1]).toContain('ch 6 note 0');
			expect(dispatches[1]).toContain('"pressed":false');
			// An unavailable pad cannot start a hold or dispatch an engine action.
			await button.click({ force: true });
			expect(dispatches).toHaveLength(2);
			await page.getByLabel('Left pad layer', { exact: true }).selectOption('hot_cue');
			await button.click();
			await expect.poll(() => dispatches.length).toBe(4);
			expect(dispatches[2]).toContain('"pressed":true');
			expect(dispatches[3]).toContain('"pressed":false');
		});
	}
}

test('XDJ-AZ exposes exact independent pad wires through UI and owned inspection IPC', async ({
	page
}) => {
	await page.goto('/practice/xdj-az');
	const panel = page.getByTestId('device-replica');
	await expect(panel).toBeVisible();
	await expect(panel.locator('[data-control-id]')).toHaveCount(172);
	const left = page.getByLabel('Left pad layer', { exact: true });
	const right = page.getByLabel('Right pad layer', { exact: true });
	const pad = panel.locator('[data-control-id="d1-pad-1"]');
	const rightPad = panel.locator('[data-control-id="d2-pad-7"]');
	await expect(left.locator('option')).toHaveCount(12);
	await expect(right.locator('option')).toHaveCount(12);
	await expect(pad).toHaveAttribute('data-wire', 'ch 6 note 0');
	await panel.locator('[data-control-id="d1-pad-mode-beat-jump"] button').click();
	await expect(left).toHaveValue('beat_jump');
	await expect(pad).toHaveAttribute('data-wire', 'ch 6 note 9');
	await expect(pad).toHaveAttribute('title', /BEAT JUMP PAD 1.*ch 6 note 9/);
	await expect(right).toHaveValue('hot_cue');
	await right.selectOption('beat_jump');
	await expect(rightPad).toHaveAttribute('data-wire', 'ch 11 note 47');
	const shift = panel.locator('[data-control-id="d1-shift"] button');
	await shift.hover();
	await page.mouse.down();
	await expect(pad).toHaveAttribute('data-wire', 'ch 6 note 10');
	await expect(rightPad).toHaveAttribute('data-wire', 'ch 11 note 48');
	await page.evaluate(() => {
		const ipc = window.musicDjToolsPracticeLayers;
		if (!ipc) throw new Error('missing inspection IPC');
		const q = ipc.query();
		if (!q.shift || q.pads.find((p) => p.id === 'd1-pad-1')?.wire?.id !== 10)
			throw new Error('IPC and displayed wire disagree');
	});
	await page.mouse.up();
	await left.selectOption('slip_loop');
	await expect(pad).toHaveAttribute('data-status', 'unwired');
	await expect(pad).toHaveAttribute('data-wire', 'unavailable');
	await shift.hover();
	await page.mouse.down();
	await expect(pad).toHaveAttribute('data-wire', 'ch 6 note 8');
	await page.mouse.up();
	for (const [mode, note] of [
		['pad_fx1_page1', 4],
		['pad_fx1_page2', 5],
		['pad_fx2_page1', 6],
		['pad_fx2_page2', 7]
	] as const) {
		await left.selectOption(mode);
		await expect(pad).toHaveAttribute('data-wire', `ch 6 note ${note}`);
		await expect(pad).toHaveAttribute('data-status', 'inert');
		await shift.hover();
		await page.mouse.down();
		await expect(pad).toHaveAttribute('data-wire', 'unavailable');
		await page.mouse.up();
	}
	// Shifted physical mode buttons select the documented inspection mode too.
	await shift.hover();
	await page.mouse.down();
	await panel.locator('[data-control-id="d1-pad-mode-hot-cue"] button').dispatchEvent('click');
	await expect(left).toHaveValue('gate_cue');
	await page.mouse.up();
	await page.evaluate(() => {
		const ipc = window.musicDjToolsPracticeLayers;
		if (!ipc) throw new Error('missing inspection IPC');
		ipc.dispatch({ type: 'select', group: 'd1', mode: 'hot_cue' });
	});
	await expect(left).toHaveValue('hot_cue');
	await expect(pad).toHaveAttribute('data-wire', 'ch 6 note 0');
	await expect(right).toHaveValue('beat_jump');
	// Native selectors and physical selectors are keyboard accessible.
	await panel.locator('[data-control-id="d1-pad-mode-beat-loop"] button').focus();
	await page.keyboard.press('Enter');
	await expect(left).toHaveValue('beat_loop');
	await expect(pad).toHaveAttribute('data-wire', 'ch 6 note 2');
	// Client-side navigation releases the normal route's IPC owner.
	await page.evaluate(() => {
		const link = document.createElement('a');
		link.href = '/practice/xdj-az/compare';
		link.textContent = 'Leave practice';
		document.body.append(link);
		link.click();
	});
	await expect
		.poll(() => page.evaluate(() => window.musicDjToolsPracticeLayers === undefined))
		.toBe(true);
	await expect(page.getByTestId('device-replica')).toBeVisible();
	await expect(page.getByLabel('Left pad layer', { exact: true })).toBeDisabled();
	await expect(page.getByLabel('Left pad layer', { exact: true })).toBeHidden();
	await expect(page.locator('[data-control-id="d1-pad-mode-beat-jump"] button')).toBeDisabled();
});
