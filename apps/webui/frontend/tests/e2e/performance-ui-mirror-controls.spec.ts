import { expect, test, type Page } from '@playwright/test';

import { CONTROL_SELECTOR } from '../../src/lib/rb/ui-mirror-controls';

const UI_BASE = (
	process.env.PERFORMANCE_E2E_BASE_URL ??
	process.env.PERFORMANCE_E2E_UI_BASE ??
	''
).replace(/\/$/, '');

const RESTART_LOOP_LABEL = 'restart loop';

interface MirrorControlsSnapshot {
	domCount: number;
	mirrorKeyCount: number;
	controls: Record<string, 'available' | 'inert'>;
	restartLoopDomCount: number;
	restartLoopMirrorKeys: string[];
}

async function waitForIpc(page: Page): Promise<void> {
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
}

async function waitForMirrorControls(page: Page): Promise<MirrorControlsSnapshot> {
	return page.evaluate(async ({ selector, restartLoopLabel }) => {
		const readMirror = async (): Promise<Record<string, 'available' | 'inert'> | null> => {
			const response = await fetch('/api/v1/state/ui-mirror');
			if (!response.ok) return null;
			const mirror = (await response.json()) as { controls?: Record<string, 'available' | 'inert'> };
			return mirror.controls ?? null;
		};
		let controls: Record<string, 'available' | 'inert'> | null = null;
		for (let attempt = 0; attempt < 30 && controls === null; attempt += 1) {
			controls = await readMirror();
			if (controls === null) await new Promise((resolve) => setTimeout(resolve, 200));
		}
		if (controls === null) throw new Error('ui-mirror controls never published');

		const domControls = document.querySelectorAll(selector);
		const restartLoopDom = [...domControls].filter(
			(element) => element.getAttribute('aria-label') === restartLoopLabel
		);
		const restartLoopMirrorKeys = Object.keys(controls).filter((key) =>
			key === restartLoopLabel || key.startsWith(`${restartLoopLabel}#`)
		);
		return {
			domCount: domControls.length,
			mirrorKeyCount: Object.keys(controls).length,
			controls,
			restartLoopDomCount: restartLoopDom.length,
			restartLoopMirrorKeys
		};
	}, { selector: CONTROL_SELECTOR, restartLoopLabel: RESTART_LOOP_LABEL });
}

test('ui-mirror controls map is one-to-one with operable DOM controls', async ({ page }) => {
	test.setTimeout(60_000);
	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto(`${UI_BASE}/performance`);
	await waitForIpc(page);

	const snapshot = await waitForMirrorControls(page);
	expect(snapshot.domCount, 'performance surface must expose operable controls').toBeGreaterThan(0);
	expect(snapshot.mirrorKeyCount).toBe(snapshot.domCount);

	expect(snapshot.restartLoopDomCount, 'restart loop appears on each deck').toBeGreaterThanOrEqual(4);
	expect(snapshot.restartLoopMirrorKeys.length).toBe(snapshot.restartLoopDomCount);
	expect(new Set(snapshot.restartLoopMirrorKeys).size).toBe(snapshot.restartLoopDomCount);
	expect(snapshot.controls[RESTART_LOOP_LABEL]).toBeDefined();

	await expect(page.locator('[data-testid="stem-vocal-deck-1"]')).toHaveCount(1);
	await expect(page.locator('[data-testid="stem-vocal-channel-1"]')).toHaveCount(1);
	expect(snapshot.controls['stem-vocal-deck-1']).toBeDefined();
	expect(snapshot.controls['stem-vocal-channel-1']).toBeDefined();
});
