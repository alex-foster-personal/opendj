import { expect, test, type Page } from '@playwright/test';

import { BRAND_LAUNCH_DURATION_MS } from '../../../src/lib/brand-launch';
import { waitForPerformanceIpc } from './performance-ready';

/** A skin on the dark theme, or 'theme-light': the default skin on the light
 * theme (html[data-theme], a separate pref that styles the library and
 * Settings shell outside .perf-root). */
export type Skin = 'default' | 'mono-dev' | 'light' | 'theme-light';
export const SKINS: Skin[] = ['default', 'mono-dev', 'light', 'theme-light'];

const NO_MOTION = '*, *::before, *::after { transition: none !important; animation: none !important; }';

/** What each labeled pass means in the app's own preferences. */
function _prefsFor(skin: Skin): { ui_skin: 'default' | 'mono-dev' | 'light'; theme: 'dark' | 'light' } {
	if (skin === 'theme-light') return { ui_skin: 'default', theme: 'light' };
	return { ui_skin: skin, theme: 'dark' };
}

interface SkinUi {
	ui_skin: string;
	wave_palette: string;
	wave_split_master: string;
}

async function _ipcUi(page: Page): Promise<SkinUi> {
	return page.evaluate(() => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		const { ui_skin, wave_palette, wave_split_master } = ipc.query().ui;
		return { ui_skin, wave_palette, wave_split_master };
	});
}

async function _putTheme(page: Page, theme: string): Promise<void> {
	const res = await page.request.put('/api/v1/ui-prefs', { data: { theme } });
	expect(res.ok(), `PUT /api/v1/ui-prefs theme=${theme}: ${res.status()}`).toBe(true);
}

const MIRROR_PATH = '/api/v1/state/ui-mirror';

/**
 * Wait until THIS page leads the engine's UI mirror (AGENT-18), so the
 * follower banner ("Another Open DJ window is in control") is not on screen
 * when the audit samples.
 */
async function _waitForMirrorLeadership(page: Page, skin: Skin): Promise<void> {
	const accepted = await page.waitForResponse(
		(response) =>
			response.request().method() === 'PUT' &&
			new URL(response.url()).pathname === MIRROR_PATH &&
			response.status() === 202,
		{ timeout: 30_000 }
	);
	const clientId = accepted.request().headers()['x-opendj-lease'];
	expect(clientId, `${skin}: the accepted mirror PUT names its writer`).toBeTruthy();
	await expect
		.poll(
			async () => {
				const res = await page.request.get(`${MIRROR_PATH}/lease`);
				expect(res.ok(), `GET ${MIRROR_PATH}/lease: ${res.status()}`).toBe(true);
				return ((await res.json()) as { holder: string | null }).holder;
			},
			{ message: `${skin}: the engine names this page as the mirror lease holder` }
		)
		.toBe(clientId);
	await expect(page.getByTestId('tab-leader-banner'), `${skin}: follower banner after leading`).toHaveCount(0);
}

/**
 * Select a skin through the app's real preferences, never by writing DOM
 * attributes: the ui_skin goes through the performance IPC `set_skin`
 * command (setUiSkin, which persists it), and the theme through the daemon's
 * ui-prefs file the app hydrates on load. The page is then reloaded so every
 * reactive reader (uiPrefs.ui_skin, uiPrefs.theme) starts from those values.
 * Must run on /performance, where the IPC is installed.
 */
export async function applySkin(page: Page, skin: Skin): Promise<void> {
	const want = _prefsFor(skin);
	await _putTheme(page, want.theme);
	const ui = await _ipcUi(page);
	await page.evaluate(
		(cmd) => window.musicDjToolsPerformance!.dispatch(cmd as never),
		{ type: 'set_skin', ui_skin: want.ui_skin, wave_palette: ui.wave_palette, wave_split_master: ui.wave_split_master }
	);
	await page.reload({ waitUntil: 'domcontentloaded' });
	await waitForPerformanceIpc(page);
	await _waitForMirrorLeadership(page, skin);
	expect((await _ipcUi(page)).ui_skin, `${skin}: uiPrefs.ui_skin after reload`).toBe(want.ui_skin);
	await expectSkin(page, skin);
}

/**
 * Presence control for every audited page: the app itself (not this test)
 * put the skin and theme on <html>, and the skin button reads the same pref.
 */
export async function expectSkin(page: Page, skin: Skin): Promise<void> {
	const want = _prefsFor(skin);
	await expect
		.poll(() => page.evaluate(() => document.documentElement.dataset.theme ?? null), { message: `${skin}: html[data-theme]` })
		.toBe(want.theme);
	const dom = await page.evaluate(() => ({
		skin: document.documentElement.dataset.skin ?? 'default',
		button: document.querySelector('[data-skin-current]')?.getAttribute('data-skin-current') ?? null
	}));
	expect(dom.skin, `${skin}: html[data-skin]`).toBe(want.ui_skin);
	if (dom.button !== null) expect(dom.button, `${skin}: skin button`).toBe(want.ui_skin);
	// The first-run brand launch leaves only on animationend, which NO_MOTION
	// would stop: let it finish, or its opaque overlay sits over every capture.
	await expect(page.locator('.brand-launch'), `${skin}: brand launch overlay`).toHaveCount(0, {
		timeout: BRAND_LAUNCH_DURATION_MS + 5_000
	});
	await page.addStyleTag({ content: NO_MOTION });
	// Let style recalculation and any skin-driven re-render settle.
	await page.waitForTimeout(300);
}

/**
 * The theme pass writes the daemon's ui-prefs file, which outlives the
 * browser context: put back what was there, pass or fail. (ui_skin lives in
 * the context's localStorage and goes away with it.) Call at module scope.
 */
export function restoreThemeAfterEach(): void {
	let originalTheme: string | null = null;
	test.beforeEach(async ({ page }) => {
		const res = await page.request.get('/api/v1/ui-prefs');
		expect(res.ok(), `GET /api/v1/ui-prefs: ${res.status()}`).toBe(true);
		const theme = ((await res.json()) as { theme?: unknown }).theme;
		originalTheme = theme === 'dark' || theme === 'light' ? theme : null;
	});
	test.afterEach(async ({ page }) => {
		if (originalTheme !== null) await _putTheme(page, originalTheme);
	});
}
