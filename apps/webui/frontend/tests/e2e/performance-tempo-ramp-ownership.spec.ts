import { expect, test, type Page } from '@playwright/test';

const AZARA = '310f7d2431c0327bca197e7ab38525e79fbcc383';

type PerfEventRow = { kind: string; deck: number | null };

async function dispatch(page: Page, command: unknown) {
	return page.evaluate(async (message: unknown) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.dispatch(message);
	}, command);
}

async function dragDeckOneTempoFader(page: Page, finalValue: number): Promise<void> {
	const fader = page.getByRole('slider', { name: 'deck 1 pitch fader' });
	await expect(fader).toBeVisible();
	const box = await fader.boundingBox();
	if (box === null) throw new Error('deck 1 pitch fader has no bounding box');
	const x = box.x + box.width / 2;
	const startY = box.y + box.height / 2;
	const finalY = box.y + box.height * (1 - finalValue);
	await page.mouse.move(x, startY);
	await page.mouse.down();
	for (let step = 1; step <= 50; step += 1) {
		await page.mouse.move(x, startY + ((finalY - startY) * step) / 50);
	}
	await page.mouse.up();
}

async function deckOneTransportScheduleCount(page: Page): Promise<number> {
	return page.evaluate(() => {
		const read = (window as Window & { __mdtPerfLog?: () => readonly PerfEventRow[] }).__mdtPerfLog;
		if (read === undefined) throw new Error('performance timing log is not installed');
		return read().filter((row) => row.kind === 'transport-schedule' && row.deck === 1).length;
	});
}

test('a wide master burst waits for each synchronized follower ramp before the next command', async ({
	page
}) => {
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await dispatch(page, { type: 'master_volume', value: 0.1 });
	await dispatch(page, { type: 'load', deck: 1, stable_id: AZARA });
	await dispatch(page, { type: 'master', deck: 1 });
	await dispatch(page, { type: 'load', deck: 2, stable_id: AZARA });
	await dispatch(page, { type: 'pitch_range', deck: 1, range: 100 });
	await dispatch(page, { type: 'pitch_range', deck: 2, range: 100 });
	await dispatch(page, { type: 'tempo', deck: 1, ratio: 1 });
	await dispatch(page, { type: 'tempo', deck: 2, ratio: 1 });
	await dispatch(page, { type: 'play', deck: 1, playing: true });
	await dispatch(page, { type: 'play', deck: 2, playing: true });
	await expect
		.poll(() =>
			page.evaluate(() => {
				const state = window.musicDjToolsPerformance?.query();
				return (
					state?.decks[1].audible === true &&
					state.decks[2].audible === true &&
					state.decks[1].transport_pending === false &&
					state.decks[2].transport_pending === false
				);
			})
		)
		.toBe(true);

	const schedulesBeforeDrag = await deckOneTransportScheduleCount(page);
	await dragDeckOneTempoFader(page, 0.625);

	await expect
		.poll(() =>
			page.evaluate(() => {
				const state = window.musicDjToolsPerformance?.query();
				if (state === undefined) throw new Error('performance IPC disappeared');
				return {
					master: state.decks[1].pitch,
					follower: state.decks[2].pitch,
					masterPending: state.decks[1].transport_pending,
					followerPending: state.decks[2].transport_pending,
					masterError: state.decks[1].sync_error,
					followerError: state.decks[2].sync_error,
					queued: state.command_queued
				};
			})
		)
		.toEqual({
			master: 1.25,
			follower: 1.25,
			masterPending: false,
			followerPending: false,
			masterError: null,
			followerError: null,
			queued: 0
		});
	const schedulesAfterDrag = await deckOneTransportScheduleCount(page);
	expect(
		schedulesAfterDrag - schedulesBeforeDrag,
		'a 50-step physical fader drag must reach the engine through its first and latest tempo commands only'
	).toBeLessThanOrEqual(2);
});
