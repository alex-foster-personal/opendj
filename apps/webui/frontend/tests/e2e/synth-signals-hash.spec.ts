/**
 * Chromium vs node SHA-256 for DEFAULT_RECIPES (issue #10402).
 *
 * Runs under tests/e2e/playwright.synth-signals.config.ts with no dev server.
 * Folds into fx-render.spec.ts when #9774 lands.
 *
 * requirement: FIXT-12006
 *
 * [if] browser hashes differ from node [then] makeSignal is not portable to Chromium.
 * [if] browser grid JSON differs from node [then] grid math is not portable.
 */
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { expect, test } from '@playwright/test';

import { bundleSvelteHarness } from './support/svelte-harness-bundle';
import { DEFAULT_RECIPES, makeSignal, signalBytes } from '../shared/synth-signals';

const ORIGIN = 'https://synth-signals.invalid';

interface ManifestRecipe {
	recipe: Record<string, unknown>;
	bytes: number;
	sha256: string;
}

interface Manifest {
	schema_version: number;
	recipes: Record<string, ManifestRecipe>;
}

const HARNESS = `<script>
	import { onMount } from 'svelte';
	import { DEFAULT_RECIPES, makeSignal, signalBytes } from './tests/shared/synth-signals.ts';
	let out = $state('');
	onMount(async () => {
		const hashes = {};
		for (const [name, recipe] of Object.entries(DEFAULT_RECIPES)) {
			const digest = await crypto.subtle.digest('SHA-256', signalBytes(makeSignal(recipe)));
			hashes[name] = Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('');
		}
		out = JSON.stringify(hashes);
	});
</script>
<pre data-testid="synth-hashes">{out}</pre>`;

function nodeHashes(): Record<string, string> {
	const out: Record<string, string> = {};
	for (const [name, recipe] of Object.entries(DEFAULT_RECIPES)) {
		const bytes = signalBytes(makeSignal(recipe));
		out[name] = createHash('sha256').update(bytes).digest('hex');
	}
	return out;
}

function readManifest(): Manifest {
	const path = new URL('../shared/synth-signals.manifest.json', import.meta.url);
	return JSON.parse(readFileSync(path, 'utf8')) as Manifest;
}

let harnessScript = '';

test.beforeAll(async () => {
	harnessScript = await bundleSvelteHarness(HARNESS);
});

test('Chromium DEFAULT_RECIPES sha256 matches node and manifest', async ({ page }) => {
	const fromNode = nodeHashes();
	const manifest = readManifest();
	await page.route(`${ORIGIN}/**`, (route) =>
		route.fulfill({
			status: 200,
			contentType: 'text/html',
			body: '<!doctype html><html><body></body></html>'
		})
	);
	await page.goto(`${ORIGIN}/`);
	await page.addScriptTag({ content: harnessScript });
	const locator = page.getByTestId('synth-hashes');
	await expect(locator).not.toHaveText('');
	await expect(locator).toHaveText(/^\{/);
	const hashText = await locator.textContent();
	if (hashText === null) throw new Error('synth-hashes element had no text');
	const browserHashes = JSON.parse(hashText) as Record<string, string>;
	for (const name of Object.keys(DEFAULT_RECIPES)) {
		expect(browserHashes[name], name).toBe(fromNode[name]);
		expect(browserHashes[name], name).toBe(manifest.recipes[name].sha256);
	}
});

test('Chromium loop grid matches node', async ({ page }) => {
	const nodeGrid = makeSignal(DEFAULT_RECIPES.loop_124_4bars).grid;
	const gridHarness = `<script>
		import { onMount } from 'svelte';
		import { DEFAULT_RECIPES, makeSignal } from './tests/shared/synth-signals.ts';
		let out = $state('');
		onMount(() => {
			out = JSON.stringify(makeSignal(DEFAULT_RECIPES.loop_124_4bars).grid);
		});
	</script>
	<pre data-testid="synth-grid">{out}</pre>`;
	const script = await bundleSvelteHarness(gridHarness);
	await page.route(`${ORIGIN}/**`, (route) =>
		route.fulfill({
			status: 200,
			contentType: 'text/html',
			body: '<!doctype html><html><body></body></html>'
		})
	);
	await page.goto(`${ORIGIN}/`);
	await page.addScriptTag({ content: script });
	const locator = page.getByTestId('synth-grid');
	await expect(locator).not.toHaveText('');
	const gridText = await locator.textContent();
	if (gridText === null) throw new Error('synth-grid element had no text');
	const browserGrid = JSON.parse(gridText) as typeof nodeGrid;
	expect(browserGrid).toEqual(nodeGrid);
});
