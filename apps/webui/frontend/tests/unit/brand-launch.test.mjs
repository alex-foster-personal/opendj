/**
 * OPS-10 brand launch contract.
 *
 * - if the launch marker is absent then the oDj animation plays once
 * - if a completed marker is present then a later launch is immediately usable
 * - if the visible brand stops using the white italic oDj wordmark then the
 *   shipped identity no longer matches the desktop icon
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let brand;

before(async () => {
	brand = await loadTypeScriptModule('src/lib/brand-launch.ts');
});

test('a new installation plays the launch exactly once', () => {
	const values = new Map();
	const storage = {
		getItem: (key) => values.get(key) ?? null,
		setItem: (key, value) => values.set(key, value)
	};

	assert.equal(brand.shouldPlayBrandLaunch(storage), true);
	brand.completeBrandLaunch(storage);
	assert.equal(brand.shouldPlayBrandLaunch(storage), false);
});

test('only the exact completion marker suppresses the launch', () => {
	const storage = { getItem: () => 'partial', setItem: () => undefined };
	assert.equal(brand.shouldPlayBrandLaunch(storage), true);
});

test('the app shell mounts a non-blocking oDj launch and wordmark', () => {
	const root = fileURLToPath(new URL('../..', import.meta.url));
	const layout = readFileSync(`${root}/src/routes/+layout.svelte`, 'utf8');
	const launch = readFileSync(`${root}/src/lib/components/BrandLaunch.svelte`, 'utf8');
	const wordmark = readFileSync(`${root}/src/lib/components/OdjWordmark.svelte`, 'utf8');

	assert.match(layout, /<BrandLaunch \/>/);
	assert.match(layout, /<OdjWordmark/);
	assert.match(launch, /pointer-events:\s*none/);
	assert.match(launch, /animationend/);
	assert.match(wordmark, /font-style:\s*italic/);
	assert.match(wordmark, />oDj</);
});
