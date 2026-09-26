/**
 * STAGE overlay lazy chunk (library bundle over budget after #4039).
 *
 * [if] the root layout still imports StageOverlay.svelte statically [then] the
 * library first paint carries it again and the budget goes red
 * [if] the chunk fails to load [then] the failure is reported AND the stage is
 * closed, so the next Stage press is a real open instead of a silent no-op
 * [if] a second open follows a failed load [then] it imports again and reports
 * again rather than awaiting the first rejection in silence
 * [if] the chunk loads [then] repeated opens reuse the one promise (one fetch)
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { build } from 'esbuild';

import { importBundledSource } from './import-bundled-source.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const LIB_ROOT = fileURLToPath(new URL('../../src/lib', import.meta.url));

function read(relative) {
	return readFileSync(fileURLToPath(new URL(`../../${relative}`, import.meta.url)), 'utf8');
}

/**
 * Bundle the loader together with the stage store it reads, so the test drives
 * the SAME stageState the loader closes. The loader's one `import()` of the
 * component is routed to `globalThis.__importStageOverlay`, so each attempt's
 * outcome is the test's to choose: a rejection is exactly how a chunk that
 * cannot be fetched surfaces to the loader.
 */
async function loadHarness(importStageOverlay) {
	const DYNAMIC_IMPORT = "const attempt = import('./StageOverlay.svelte')";
	const routeDynamicImport = {
		name: 'route-stage-overlay-import',
		setup(pluginBuild) {
			pluginBuild.onLoad({ filter: /stage-overlay-loader\.ts$/ }, (args) => {
				const source = readFileSync(args.path, 'utf8');
				const sites = source.split(DYNAMIC_IMPORT).length - 1;
				if (sites !== 1) throw new Error(`expected one ${DYNAMIC_IMPORT}, found ${sites}`);
				return {
					contents: source.replace(DYNAMIC_IMPORT, 'const attempt = globalThis.__importStageOverlay()'),
					loader: 'ts'
				};
			});
		}
	};
	const result = await build({
		stdin: {
			contents: `export * from '$lib/components/lyrics/stage-overlay-loader';
			           export { openStage } from '$lib/lyrics/stage-store.svelte';`,
			resolveDir: FRONTEND_ROOT,
			loader: 'ts'
		},
		absWorkingDir: FRONTEND_ROOT,
		alias: { $lib: LIB_ROOT },
		bundle: true,
		define: { $state: 'globalThis.__musicDjToolsTestState' },
		format: 'esm',
		logLevel: 'silent',
		platform: 'node',
		plugins: [routeDynamicImport],
		target: 'node20',
		write: false
	});
	globalThis.__musicDjToolsTestState = (value) => value;
	const attempts = { count: 0 };
	globalThis.__importStageOverlay = () => {
		attempts.count += 1;
		return importStageOverlay();
	};
	const mod = await importBundledSource(result.outputFiles[0].text, 'stage-overlay-loader');
	return { mod, attempts };
}

const chunkFetchFails = () =>
	Promise.reject(new TypeError('Failed to fetch dynamically imported module'));

test('root layout mounts the stage from the lazy loader, never a static import', () => {
	const layout = read('src/routes/+layout.svelte');
	assert.doesNotMatch(layout, /import StageOverlay from/);
	assert.match(layout, /from '\$lib\/components\/lyrics\/stage-overlay-loader'/);
	assert.match(layout, /\{#if isStageOverlayOpen\(\)\}/);
	assert.match(layout, /loadStageOverlay\(reportStageOverlayLoadFailure\)/);
	assert.match(layout, /prefetchStageOverlay\(reportStageOverlayLoadFailure\)/);
});

test('/performance takes the stage chunk with its own first paint', () => {
	const page = read('src/routes/performance/+page.svelte');
	assert.match(page, /import '\$lib\/components\/lyrics\/StageOverlay\.svelte';/);
});

test('if the chunk fails [then] it is reported and the stage closes', async () => {
	const { mod, attempts } = await loadHarness(chunkFetchFails);
	mod.openStage('track-1');
	assert.equal(mod.isStageOverlayOpen(), true);
	const reported = [];
	await assert.rejects(mod.loadStageOverlay((error) => reported.push(error)));
	assert.equal(reported.length, 1, 'one failure, one report');
	assert.match(String(reported[0]), /Failed to fetch dynamically imported module/);
	assert.equal(mod.isStageOverlayOpen(), false, 'a failed chunk must not leave an invisible open stage');
});

test('if the stage is reopened after a failure [then] it imports and reports again', async () => {
	const { mod, attempts } = await loadHarness(chunkFetchFails);
	const reported = [];
	mod.openStage('track-1');
	await assert.rejects(mod.loadStageOverlay((error) => reported.push(error)));
	mod.openStage('track-1');
	assert.equal(mod.isStageOverlayOpen(), true, 'the next press is a real open');
	await assert.rejects(mod.loadStageOverlay((error) => reported.push(error)));
	assert.equal(reported.length, 2, 'the second open reports its own failure');
	assert.equal(attempts.count, 2, 'the second open is a second import, not the cached rejection');
});

test('if the chunk loads [then] every open reuses the one promise', async () => {
	const { mod, attempts } = await loadHarness(async () => ({ default: function StageOverlayStub() {} }));
	const reported = [];
	const first = mod.loadStageOverlay((error) => reported.push(error));
	const second = mod.loadStageOverlay((error) => reported.push(error));
	assert.equal(first, second, 'one import, not one per open');
	const loaded = await first;
	assert.equal(typeof loaded.default, 'function');
	assert.equal(attempts.count, 1);
	assert.deepEqual(reported, []);
});
