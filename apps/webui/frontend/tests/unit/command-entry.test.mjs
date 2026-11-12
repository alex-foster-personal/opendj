/**
 * #13117: keyboard focus reaches text command entry and Tab advances.
 * Chromium is the default; set COMMAND_ENTRY_BROWSER=webkit for WebKit.
 * Browser binaries are required, never skipped.
 */
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { dirname } from 'node:path';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { chromium, webkit } from 'playwright';
import { compile } from 'svelte/compiler';

import { build } from './esbuild-tsconfig.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../../', import.meta.url));
const LIB_ROOT = fileURLToPath(new URL('../../src/lib', import.meta.url));
const BROWSER_NAME = process.env.COMMAND_ENTRY_BROWSER ?? 'chromium';
let browser;
let bundle;

before(async () => {
	assert.ok(
		['chromium', 'webkit'].includes(BROWSER_NAME),
		'COMMAND_ENTRY_BROWSER must be chromium or webkit'
	);
	const engine = BROWSER_NAME === 'chromium' ? chromium : webkit;
	assert.ok(
		existsSync(engine.executablePath()),
		`Missing ${BROWSER_NAME}; run pnpm exec playwright install ${BROWSER_NAME} in apps/webui/frontend`
	);
	const result = await build({
		stdin: {
			contents: `
				import CommandEntry from '$lib/components/rb/CommandEntry.svelte';
				import { mount, unmount, flushSync } from 'svelte';
				let component;
				window.commandEntryHarness = {
					mount() {
						flushSync(() => {
							component = mount(CommandEntry, {
								target: document.getElementById('target')
							});
						});
					},
					async unmount() { await unmount(component); }
				};`,
			resolveDir: FRONTEND_ROOT,
			loader: 'js'
		},
		alias: { $lib: LIB_ROOT },
		bundle: true,
		platform: 'browser',
		conditions: ['browser'],
		format: 'iife',
		write: false,
		define: {
			'import.meta.env.VITE_API_BASE': JSON.stringify('https://command-entry-harness.example.test'),
			'import.meta.env.DEV': 'false'
		},
		plugins: [
			{
				name: 'command-entry-svelte-client',
				setup(builder) {
					builder.onLoad({ filter: /\.svelte$/ }, async ({ path }) => ({
						contents: compile(await readFile(path, 'utf8'), {
							filename: path,
							generate: 'client',
							css: 'injected'
						}).js.code,
						loader: 'js',
						resolveDir: dirname(path)
					}));
				}
			}
		]
	});
	bundle = result.outputFiles[0].text;
	browser = await engine.launch({ headless: true });
});

after(async () => {
	await browser?.close();
});

async function mountEntry(t) {
	const page = await browser.newPage();
	const pageErrors = [];
	page.on('pageerror', (error) => pageErrors.push(error.message));
	let mounted = false;
	t.after(async () => {
		try {
			if (mounted) await page.evaluate(() => window.commandEntryHarness.unmount());
		} finally {
			await page.close();
		}
		assert.deepEqual(pageErrors, [], 'component must not throw browser errors');
	});
	const fixtureTokens =
		'<style>:root{--rb-focus-ring:#4d9fff;--rb-well:#0a0c0f;--rb-border:#333;' +
		'--rb-text:#fff;--rb-font:sans-serif;--rb-fs-label:11px;--rb-text-dim:#888}</style>';
	await page.setContent(
		`${fixtureTokens}<button id="before">before</button><div id="target"></div><button id="after">after</button>`
	);
	await page.addScriptTag({ content: bundle });
	await page.evaluate(() => window.commandEntryHarness.mount());
	mounted = true;
	return page;
}

function commandInput(page) {
	return page.locator('input[aria-label="text command entry"]');
}

test('Tab focuses text command entry with visible focus ring', async (t) => {
	const page = await mountEntry(t);
	await page.keyboard.press('Tab');
	await page.keyboard.press('Tab');
	const input = commandInput(page);
	await input.waitFor({ state: 'visible' });
	assert.equal(await input.evaluate((element) => document.activeElement === element), true);
	const styles = await input.evaluate((element) => {
		const computed = getComputedStyle(element);
		return {
			focusVisible: element.matches(':focus-visible'),
			outlineWidth: computed.outlineWidth,
			outlineStyle: computed.outlineStyle
		};
	});
	assert.equal(styles.focusVisible, true);
	assert.equal(styles.outlineWidth, '2px');
	assert.equal(styles.outlineStyle, 'solid');
});

test('Tab advances from command entry to the next control', async (t) => {
	const page = await mountEntry(t);
	await page.keyboard.press('Tab');
	await page.keyboard.press('Tab');
	const input = commandInput(page);
	assert.equal(await input.evaluate((element) => document.activeElement === element), true);
	await page.keyboard.press('Tab');
	const after = page.locator('#after');
	assert.equal(await after.evaluate((element) => document.activeElement === element), true);
});
