/**
 * Dev hot updates of a module that installs an install-once port at load.
 *
 * Regression lines:
 * - if a hot update of performance-ipc / audio-engine re-runs its install and
 *   throws "already installed" then broken (CORE Mon 5 Oct 2026: every
 *   importer of performance-ipc failed to hot-reload until a full reload)
 * - if a second install with no release succeeds then broken: production
 *   keeps the once-only guard (this is the mutation control)
 * - if an uninstall leaves the slot held then broken
 * - if performance-ipc or audio-engine stops routing these installs through
 *   reinstallAcrossHotUpdates then broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'http://127.0.0.1:9';

/** A stand-in for `import.meta.hot`: only `data` is read, and Vite keeps the
 * same object across every re-run of one module. */
function fakeHot() {
	return { data: {} };
}

const PORTS = [
	{
		module: 'src/lib/rb/analysis-source.svelte.ts',
		install: 'installAnalysisSourceRefreshRunner',
		already: /an analysis source refresh runner is already installed/,
		port: () => async (work) => work()
	},
	{
		module: 'src/lib/components/rb/wave/anlz-cache.svelte.ts',
		install: 'installAuthoritativeAnlzGridSink',
		already: /an authoritative anlz grid sink is already installed/,
		port: () => () => {}
	},
	{
		module: 'src/lib/components/rb/wave/anlz-cache.svelte.ts',
		install: 'installAuthoritativeAnlzErrorSink',
		already: /an authoritative anlz error sink is already installed/,
		port: () => () => {}
	}
];

async function loadHelper() {
	return loadTypeScriptModule('src/lib/rb/hmr-reinstall.ts');
}

for (const { module, install, already, port } of PORTS) {
	test(`${install}: install, uninstall, install again succeeds`, async () => {
		const target = await loadTypeScriptModule(module, { viteApiBase: API_BASE });
		const release = target[install](port());
		assert.equal(typeof release, 'function', 'install must return its uninstall');
		release();
		const releaseAgain = target[install](port());
		assert.equal(typeof releaseAgain, 'function');
	});

	test(`${install}: a second install with no uninstall still throws`, async () => {
		const target = await loadTypeScriptModule(module, { viteApiBase: API_BASE });
		target[install](port());
		assert.throws(() => target[install](port()), already);
	});

	test(`${install}: a hot re-run through reinstallAcrossHotUpdates does not throw`, async () => {
		const target = await loadTypeScriptModule(module, { viteApiBase: API_BASE });
		const { reinstallAcrossHotUpdates } = await loadHelper();
		const hot = fakeHot();
		const first = port();
		const second = port();
		reinstallAcrossHotUpdates(hot, 'slot', () => target[install](first));
		// The installing module re-runs: same hot.data, same target instance.
		reinstallAcrossHotUpdates(hot, 'slot', () => target[install](second));
		// The slot now belongs to the second port: a third install still guards.
		assert.throws(() => target[install](port()), already);
		hot.data.slot();
		assert.equal(typeof target[install](port()), 'function');
	});

	test(`${install}: with no hot context (production) a re-run still throws`, async () => {
		const target = await loadTypeScriptModule(module, { viteApiBase: API_BASE });
		const { reinstallAcrossHotUpdates } = await loadHelper();
		reinstallAcrossHotUpdates(undefined, 'slot', () => target[install](port()));
		assert.throws(
			() => reinstallAcrossHotUpdates(undefined, 'slot', () => target[install](port())),
			already
		);
	});
}

test('the module-scope installers route through reinstallAcrossHotUpdates', () => {
	const read = (path) => readFileSync(fileURLToPath(new URL(`../../${path}`, import.meta.url)), 'utf8');
	const wiring = [
		['src/lib/rb/performance-ipc.svelte.ts', 'installAnalysisSourceRefreshRunner'],
		['src/lib/rb/audio-engine.svelte.ts', 'installAuthoritativeAnlzGridSink'],
		['src/lib/rb/audio-engine.svelte.ts', 'installAuthoritativeAnlzErrorSink']
	];
	for (const [path, install] of wiring) {
		const source = read(path);
		const pattern = new RegExp(
			`reinstallAcrossHotUpdates\\(import\\.meta\\.hot, '\\w+', \\(\\) =>\\s+${install}\\(`
		);
		assert.match(source, pattern, `${path} must install ${install} through reinstallAcrossHotUpdates`);
		const bare = new RegExp(`^${install}\\(`, 'm');
		assert.doesNotMatch(source, bare, `${path} has a bare module-scope ${install} call`);
	}
});
