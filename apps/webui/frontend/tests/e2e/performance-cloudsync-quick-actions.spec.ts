// requirement: CSUI-02
// [if] a deck is playing on /performance [then] clicking CloudSync opens quick actions without route or transport change, [else stop]

import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import type {
	PerformanceCommand,
	PerformanceState
} from '../../src/lib/rb/performance-ipc.svelte';

declare global {
	interface Window {
		__cs3531Commands?: PerformanceCommand[];
	}
}

const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const DATA_DIR = process.env.MDT_DATA_DIR ?? join(REPOSITORY_ROOT, 'data');
const IS_GENERATED_FIXTURE = existsSync(join(DATA_DIR, 'fixture-revision.txt'));

const API_BASE = process.env.PERFORMANCE_E2E_API_BASE ?? 'http://127.0.0.1:8686';
const UI_BASE = (
	process.env.PERFORMANCE_E2E_BASE_URL ??
	process.env.PERFORMANCE_E2E_UI_BASE ??
	''
).replace(/\/$/, '');

interface TrackWire {
	stable_id: string;
	bpm: number | null;
	file_exists: boolean;
}

function _skipOrFailSmoke(condition: boolean, reason: string): void {
	if (!condition) return;
	if (process.env.PERFORMANCE_E2E_SMOKE === '1') {
		throw new Error(`performance-smoke fail-fast: ${reason}`);
	}
	test.skip(true, reason);
}

async function _waitForIpc(page: Page): Promise<void> {
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
}

async function _gotoPerformance(page: Page): Promise<void> {
	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto(`${UI_BASE}/performance`);
	await _waitForIpc(page);
	await _dispatch(page, { type: 'master_volume', value: 0.1 });
}

async function _query(page: Page): Promise<PerformanceState> {
	await _waitForIpc(page);
	return page.evaluate(() => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.query();
	});
}

async function _dispatch(page: Page, command: PerformanceCommand): Promise<PerformanceState> {
	await _waitForIpc(page);
	return page.evaluate(async (message) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.dispatch(message);
	}, command);
}

async function _fetchPlayableTrack(request: APIRequestContext): Promise<TrackWire> {
	_skipOrFailSmoke(
		IS_GENERATED_FIXTURE,
		`generated fixture at ${DATA_DIR}: run with PERFORMANCE_E2E_FIXTURE=0 against an analyzed library`
	);
	const tracksResponse = await request.get(`${API_BASE}/api/v1/tracks?limit=100&available=true`);
	expect(tracksResponse.ok(), 'real available-track listing must succeed').toBeTruthy();
	const payload = (await tracksResponse.json()) as { items: TrackWire[] };
	const track = payload.items.find(
		(row): row is TrackWire & { bpm: number } =>
			row.file_exists && typeof row.bpm === 'number' && row.bpm > 0
	);
	_skipOrFailSmoke(track === undefined, 'requires a real library with at least one analyzed track');
	return track!;
}

function _transportStopOrPause(command: PerformanceCommand): boolean {
	return (command.type === 'play' && command.playing === false) || command.type === 'rescue_stop_all';
}

async function _installCommandSpy(page: Page): Promise<void> {
	await page.evaluate(() => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		const original = ipc.dispatch.bind(ipc);
		window.__cs3531Commands = [];
		window.musicDjToolsPerformance = Object.freeze({
			...ipc,
			dispatch: async (message: unknown, pressT0Ms?: number) => {
				window.__cs3531Commands!.push(message as PerformanceCommand);
				return original(message, pressT0Ms);
			}
		});
	});
}

async function _commandsSince(page: Page, since: number): Promise<PerformanceCommand[]> {
	return page.evaluate(({ start }) => window.__cs3531Commands!.slice(start), { start: since });
}

test(
	'CloudSync quick actions keep /performance playing',
	{ tag: '@performance-smoke' },
	async ({ page, request }) => {
		const track = await _fetchPlayableTrack(request);
		await _gotoPerformance(page);
		await _dispatch(page, { type: 'load', deck: 1, stable_id: track.stable_id });
		await _dispatch(page, { type: 'play', deck: 1, playing: true });
		await expect.poll(async () => (await _query(page)).decks[1].audible).toBe(true);

		await _installCommandSpy(page);

		const urlBefore = page.url();
		const chip = page.getByRole('button', { name: 'CloudSync status' });
		const commandCountBefore = await page.evaluate(() => window.__cs3531Commands!.length);
		await chip.click();
		const activationCommands = await _commandsSince(page, commandCountBefore);
		expect(activationCommands.filter(_transportStopOrPause)).toEqual([]);
		await expect(page.getByTestId('cloudsync-quick-actions-popover')).toBeVisible();
		expect(page.url()).toBe(urlBefore);
		expect(urlBefore).toMatch(/\/performance/);

		const afterOpen = await _query(page);
		expect(afterOpen.decks[1].playing).toBe(true);
		expect(afterOpen.decks[1].audible).toBe(true);

		await page.keyboard.press('Escape');
		await expect(page.getByTestId('cloudsync-quick-actions-popover')).toBeHidden();
		expect(page.url()).toBe(urlBefore);

		const dismissCommandCount = await page.evaluate(() => window.__cs3531Commands!.length);
		await chip.click();
		const reopenCommands = await _commandsSince(page, dismissCommandCount);
		expect(reopenCommands.filter(_transportStopOrPause)).toEqual([]);
		await expect(page.getByTestId('cloudsync-quick-actions-popover')).toBeVisible();
		await page.mouse.click(8, 8);
		await expect(page.getByTestId('cloudsync-quick-actions-popover')).toBeHidden();

		const afterDismiss = await _query(page);
		expect(afterDismiss.decks[1].playing).toBe(true);
		expect(afterDismiss.decks[1].audible).toBe(true);

		await chip.click();
		await page.getByTestId('cloudsync-quick-advanced').click();
		await expect(page).toHaveURL(/\/cloudsync/);
	}
);
