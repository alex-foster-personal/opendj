/**
 * #12428: exercise shipped StemRow buttons with native browser input.
 * Enter/Space must match pointer mute exactly, never solo without Shift.
 * Unavailable and not-ready chips must reject focus and keyboard activation.
 * Run with STEM_ROW_BROWSER=webkit for the same checks in Linux WebKit;
 * Chromium is the default. Browser binaries are required, never skipped.
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
const BROWSER_NAME = process.env.STEM_ROW_BROWSER ?? 'chromium';
const HARNESS_URL = 'http://stem-row.harness.test/';
const EMPTY_CALLS = [];
const MUTE_CALLS = [{ action: 'mute', stem: 'vocal' }];
let browser;
let bundle;

before(async () => {
	assert.ok(
		['chromium', 'webkit'].includes(BROWSER_NAME),
		'STEM_ROW_BROWSER must be chromium or webkit'
	);
	const engine = BROWSER_NAME === 'chromium' ? chromium : webkit;
	assert.ok(
		existsSync(engine.executablePath()),
		`Missing ${BROWSER_NAME}; run pnpm exec playwright install ${BROWSER_NAME} in apps/webui/frontend`
	);
	const result = await build({
		stdin: {
			contents: `
				import StemRow from '$lib/components/rb/deck/StemRow.svelte';
				import { mount, unmount, flushSync } from 'svelte';
				let component;
				window.stemRowHarness = {
					calls: [],
					mount(deck) {
						flushSync(() => {
							component = mount(StemRow, {
								target: document.getElementById('target'),
								props: {
									deck, pending: false,
									onMute: async (stem) => { this.calls.push({ action: 'mute', stem }); },
									onSolo: async (stem) => { this.calls.push({ action: 'solo', stem }); }
								}
							});
						});
					},
					async unmount() { await unmount(component); }
				};`,
			resolveDir: FRONTEND_ROOT,
			loader: 'js'
		},
		alias: { $lib: LIB_ROOT },
		// StemRow's wheel-adjust import reaches api/base.ts, which reads import.meta.env at
		// module scope; the iife bundle has no Vite env, so define it like the other harnesses.
		define: { 'import.meta.env.VITE_API_BASE': 'undefined', 'import.meta.env.DEV': 'false' },
		bundle: true,
		platform: 'browser',
		conditions: ['browser'],
		format: 'iife',
		write: false,
		plugins: [
			{
				name: 'stem-row-svelte-client',
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

// Synthesized component props, matching StemRow.contract.ts. Only deck_id
// and stems are read by the component; no engine or audio output is faked.
function deck(stems = {}) {
	return {
		deck_id: 1,
		stems: {
			status: 'ready',
			source: 'demucs',
			model: 'htdemucs',
			layout: 'demucs4',
			available_controls: ['vocal', 'instrumental', 'drums', 'bass', 'other'],
			alignment: null,
			error: null,
			load: null,
			controls: Object.fromEntries(
				['vocal', 'instrumental', 'drums', 'bass', 'other'].map((stem) => [
					stem,
					{ muted: false, solo: false, gain: 0.5 }
				])
			),
			...stems
		}
	};
}

async function mountRow(t, stems = {}) {
	const page = await browser.newPage();
	const pageErrors = [];
	const consoleErrors = [];
	page.on('pageerror', (error) => pageErrors.push(error.message));
	page.on('console', (message) => {
		if (message.type() === 'error') consoleErrors.push(message.text());
	});
	let mounted = false;
	t.after(async () => {
		try {
			if (mounted) await page.evaluate(() => window.stemRowHarness.unmount());
		} finally {
			await page.close();
		}
		assert.deepEqual(pageErrors, [], 'component must not throw browser errors');
	});
	// A real (routed, offline) origin, not about:blank: StemRow's wheel-adjust import reads
	// window.localStorage at module load, which an about:blank document refuses.
	await page.route(HARNESS_URL, (route) =>
		route.fulfill({
			contentType: 'text/html',
			body: '<!doctype html><button id="before">Before stems</button><div id="target"></div>'
		})
	);
	await page.goto(HARNESS_URL);
	await page.addScriptTag({ content: bundle });
	if (!(await page.evaluate(() => window.stemRowHarness !== undefined))) {
		assert.fail(
			`stem-row harness bundle did not define window.stemRowHarness; ` +
				`page errors: ${JSON.stringify(pageErrors)}; console errors: ${JSON.stringify(consoleErrors)}`
		);
	}
	await page.evaluate((props) => window.stemRowHarness.mount(props), deck(stems));
	mounted = true;
	return page;
}

async function calls(page) {
	return page.evaluate(() => window.stemRowHarness.calls);
}

async function activatedCalls(page) {
	await page.waitForFunction(() => window.stemRowHarness.calls.length > 0);
	return calls(page);
}

async function assertNativeButton(chip, disabled) {
	assert.equal(await chip.evaluate((element) => element instanceof HTMLButtonElement), true);
	assert.equal(await chip.isDisabled(), disabled);
}

async function assertFocused(locator, expected = true) {
	assert.equal(await locator.evaluate((element) => document.activeElement === element), expected);
}

async function pointerMute(t) {
	const page = await mountRow(t);
	const chip = page.getByTestId('stem-vocal-deck-1');
	await assertNativeButton(chip, false);
	await chip.click();
	const trace = await activatedCalls(page);
	assert.deepEqual(trace, MUTE_CALLS);
	return trace;
}

test('ready VOCAL pointer activation mutes exactly once without solo', async (t) => {
	await pointerMute(t);
});

for (const key of ['Enter', 'Space']) {
	test(`ready VOCAL ${key} activation matches pointer mute without solo`, async (t) => {
		const pointerTrace = await pointerMute(t);
		const page = await mountRow(t);
		const chip = page.getByTestId('stem-vocal-deck-1');
		await assertNativeButton(chip, false);
		await chip.focus();
		await assertFocused(chip);
		await page.keyboard.down(key);
		if (key === 'Space')
			assert.deepEqual(await calls(page), EMPTY_CALLS, 'Space activates on release');
		await page.keyboard.up(key);
		assert.deepEqual(await activatedCalls(page), pointerTrace);
	});
}

test('ready VOCAL Shift-click selects solo exactly once without mute', async (t) => {
	const page = await mountRow(t);
	await page.getByTestId('stem-vocal-deck-1').click({ modifiers: ['Shift'] });
	assert.deepEqual(await activatedCalls(page), [{ action: 'solo', stem: 'vocal' }]);
});

async function assertKeyboardInert(page, chip) {
	await assertNativeButton(chip, true);
	const neutral = page.locator('#before');
	await neutral.focus();
	await chip.focus();
	await assertFocused(chip, false);
	await assertFocused(neutral);
	for (const key of ['Enter', 'Space']) {
		await page.keyboard.press(key);
		assert.deepEqual(await calls(page), EMPTY_CALLS);
	}
}

test('unavailable RoFormer DRUMS ignores keyboard input and is skipped by Tab', async (t) => {
	const page = await mountRow(t, {
		layout: 'roformer2',
		model: 'roformer',
		available_controls: ['vocal', 'instrumental']
	});
	const chip = page.getByTestId('stem-drums-deck-1');
	assert.equal(await chip.getAttribute('data-unavailable'), 'true');
	await assertKeyboardInert(page, chip);
	for (const testId of [
		'stem-vocal-deck-1',
		'stem-instrumental-deck-1',
		'track-load-reset-deck-1'
	]) {
		await page.keyboard.press('Tab');
		await assertFocused(page.getByTestId(testId));
	}
	assert.deepEqual(await calls(page), EMPTY_CALLS);
});

test('not-ready VOCAL ignores keyboard input and is skipped by Tab', async (t) => {
	const page = await mountRow(t, { status: 'loading' });
	const chip = page.getByTestId('stem-vocal-deck-1');
	assert.equal(await chip.getAttribute('data-unavailable'), 'false');
	await assertKeyboardInert(page, chip);
	await page.keyboard.press('Tab');
	await assertFocused(page.getByTestId('track-load-reset-deck-1'));
	assert.deepEqual(await calls(page), EMPTY_CALLS);
});
