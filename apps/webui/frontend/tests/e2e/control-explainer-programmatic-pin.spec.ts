/**
 * ControlExplainer programmatic pin, driven in a real browser (CHROME-07).
 *
 * Codex P2 on PR #3896 (comment 4129520600): the programmaticOpen effect only
 * handled the rising edge. When openMidiDrawer() unpinned the I/O view
 * (ioSurface.open -> false) the explainer's own open/pinned stayed set, and
 * the I/O dialog kept covering the MIDI drawer.
 *
 * This compiles the REAL ControlExplainer.svelte with Svelte's client compiler,
 * mounts it in Chromium under a tiny parent that owns programmaticOpen the way
 * HeadphoneCluster does, and drives it with real pointer input. No stubs: the
 * unit suite has no DOM, and an effect's edge behavior is exactly what a
 * source regex cannot establish.
 *
 * Harness-agnostic (setContent, nothing read at module scope); it runs in
 * the root suite (e2e.yml "Root Playwright suite"). The real /performance
 * tray -> I/O -> MIDI flow is io-midi-drawer-handoff.spec.ts; this file is its
 * component-level complement for the edges that flow does not reach.
 *
 * [if] the parent drops programmaticOpen [then] the popover closes and
 *   onProgrammaticClose is NOT called again [else stop].
 * control [if] a user click-pins with programmaticOpen false throughout
 *   [then] it stays open [else stop if the fix closes user pins].
 * control [if] an outside click dismisses a programmatic pin [then]
 *   onProgrammaticClose runs exactly once [else stop].
 */
// requirement: CHROME-07
import { expect, test, type Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

import { build } from 'esbuild';
import { compile } from 'svelte/compiler';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const LIB_ROOT = fileURLToPath(new URL('../../src/lib', import.meta.url));

// The parent mirrors HeadphoneCluster: it owns the flag and clears it from
// onProgrammaticClose. __harness only exposes that same flag to the test.
const HARNESS = `<script>
	import ControlExplainer from '$lib/components/rb/deck/ControlExplainer.svelte';
	let prog = $state(false);
	let closes = $state(0);
	window.__harness = { setProg: (v) => (prog = v), closes: () => closes };
</script>
<ControlExplainer
	title="Audio I/O"
	bullets={['Output device', 'Headphone device']}
	placement="below"
	pinOnClick
	programmaticOpen={prog}
	onProgrammaticClose={() => {
		closes += 1;
		prog = false;
	}}
>
	<button id="trigger" type="button">I/O</button>
</ControlExplainer>
<button id="outside" type="button" style="position:fixed;right:8px;bottom:8px">outside</button>`;

async function bundle(): Promise<string> {
	const result = await build({
		stdin: {
			contents:
				"import { mount } from 'svelte';\nimport Harness from 'virtual:harness.svelte';\nmount(Harness, { target: document.body });",
			resolveDir: FRONTEND_ROOT,
			loader: 'js'
		},
		absWorkingDir: FRONTEND_ROOT,
		alias: { $lib: LIB_ROOT },
		bundle: true,
		conditions: ['browser'],
		format: 'iife',
		logLevel: 'silent',
		platform: 'browser',
		write: false,
		plugins: [
			{
				name: 'svelte-client',
				setup(b) {
					b.onResolve({ filter: /^virtual:harness\.svelte$/ }, () => ({
						path: 'harness.svelte',
						namespace: 'harness'
					}));
					b.onLoad({ filter: /.*/, namespace: 'harness' }, () => ({
						contents: compile(HARNESS, { filename: 'Harness.svelte', generate: 'client' }).js.code,
						loader: 'js',
						resolveDir: FRONTEND_ROOT
					}));
					b.onLoad({ filter: /\.svelte$/ }, async (args) => {
						const source = await readFile(args.path, 'utf8');
						const out = compile(source, { filename: args.path, generate: 'client', css: 'injected' });
						return { contents: out.js.code, loader: 'js', resolveDir: dirname(args.path) };
					});
				}
			}
		]
	});
	return result.outputFiles[0].text;
}

let script = '';

test.beforeAll(async () => {
	script = await bundle();
});

async function mountHarness(page: Page): Promise<void> {
	await page.setContent('<!doctype html><html><body style="margin:40px"></body></html>');
	await page.addScriptTag({ content: script });
	await expect(page.locator('#trigger')).toBeVisible();
}

const pop = (page: Page) => page.locator('.explainer .pop');
const closes = (page: Page) =>
	page.evaluate(() => (window as unknown as { __harness: { closes(): number } }).__harness.closes());
const setProg = (page: Page, v: boolean) =>
	page.evaluate(
		(value) => (window as unknown as { __harness: { setProg(x: boolean): void } }).__harness.setProg(value),
		v
	);

test.describe('ControlExplainer programmatic pin', () => {
	test('the parent dropping programmaticOpen closes the pinned popover without a second close callback', async ({
		page
	}) => {
		await mountHarness(page);
		await setProg(page, true);
		await expect(pop(page)).toBeVisible();
		await setProg(page, false);
		await expect(pop(page)).toHaveCount(0);
		expect(await closes(page)).toBe(0);
	});

	test('control: a user click-pin stays open while programmaticOpen stays false', async ({ page }) => {
		await mountHarness(page);
		await page.click('#trigger');
		await expect(pop(page)).toBeVisible();
		// Leave the trigger: an unpinned hover popover would close here.
		await page.mouse.move(2, 2);
		await page.waitForTimeout(400);
		await expect(pop(page)).toBeVisible();
		expect(await closes(page)).toBe(0);
	});

	test('control: an outside click dismisses a programmatic pin and reports it exactly once', async ({
		page
	}) => {
		await mountHarness(page);
		await setProg(page, true);
		await expect(pop(page)).toBeVisible();
		await page.click('#outside');
		await expect(pop(page)).toHaveCount(0);
		expect(await closes(page)).toBe(1);
	});
});
