import { fileURLToPath } from 'node:url';

import { build } from 'esbuild';
import { compileModule } from 'svelte/compiler';

import { importBundledSource } from './import-bundled-source.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const LIB_ROOT = fileURLToPath(new URL('../../src/lib', import.meta.url));

/**
 * Vite's `?url` suffix, modelled for the test bundler. Same contract as the one
 * in load-typescript.mjs: the import resolves to the path as a string, and
 * nothing under test may depend on the value.
 */
const urlSuffixImports = {
	name: 'vite-url-suffix',
	setup(build) {
		build.onResolve({ filter: /\?url$/ }, (args) => ({
			path: args.path,
			namespace: 'vite-url-suffix'
		}));
		build.onLoad({ filter: /.*/, namespace: 'vite-url-suffix' }, (args) => ({
			contents: `export default ${JSON.stringify(args.path.replace(/\?url$/, ''))};`,
			loader: 'js'
		}));
	}
};

/**
 * A data-URL test module cannot resolve a separately bundled lazy chunk.
 * Keep dynamic imports external while compiling the rune graph: tests that
 * exercise a deferred module use load-typescript.mjs, while rune tests retain
 * the production module's laziness without wrapping rune declarations in an
 * esbuild callback before Svelte sees them.
 */
const dynamicImportExternal = {
	name: 'dynamic-import-external',
	setup(build) {
		build.onResolve({ filter: /.*/ }, (args) =>
			args.kind === 'dynamic-import' ? { path: args.path, external: true } : undefined
		);
	}
};

function _bundle(entry, { stdin = false, deferSvelteRuntime = false } = {}) {
	return build({
		...(stdin
			? { stdin: { contents: entry, resolveDir: FRONTEND_ROOT, loader: 'ts', sourcefile: 'rune-entry.ts' } }
			: { entryPoints: [entry] }),
		absWorkingDir: FRONTEND_ROOT,
		alias: { $lib: LIB_ROOT },
		bundle: true,
		// compileModule must see application runes, not Svelte's own runtime
		// internals. A production module may import `untrack` from `svelte`;
		// inlining that package here exposes runtime names such as `$window` to
		// the application rune compiler, which correctly rejects them. Link the
		// real runtime only in the second bundle, after rune compilation.
		...(deferSvelteRuntime ? { external: ['svelte', 'svelte/*'] } : {}),
		define: { 'import.meta.env.VITE_API_BASE': JSON.stringify('https://rune-harness.example.test') },
		format: 'esm',
		logLevel: 'silent',
		platform: 'browser',
		plugins: [urlSuffixImports, dynamicImportExternal],
		target: 'es2022',
		write: false
	});
}

/**
 * Load a `.svelte.ts` rune module with its RUNES LIVE, not stubbed.
 *
 * load-typescript.mjs defines `$state` as an identity function, which is right
 * for testing pure logic and useless for testing reactivity: an `$effect` there
 * is a syntax error waiting to happen, never a subscription. This harness
 * instead runs the real compiler.
 *
 * Three passes, and each one is load-bearing:
 *   1. esbuild the module graph into one file with the runes INTACT (no $state
 *      define), because compileModule works on source, not on a package tree.
 *   2. svelte compileModule, which is what turns $state/$effect/$effect.root
 *      into real svelte/internal/client reactivity for a module (not a
 *      component) file.
 *   3. esbuild again to inline svelte/internal/client, because a data: URL
 *      cannot resolve a bare specifier.
 *
 * `entrySource` is TypeScript evaluated at the frontend root, so it can
 * re-export from '$lib/...' - the module under test plus whatever singletons a
 * test needs to poke to drive it.
 */
export async function loadRuneModule(entrySource) {
	let bundled = _bundledBySource.get(entrySource);
	if (bundled === undefined) {
		bundled = _bundleRunes(entrySource);
		_bundledBySource.set(entrySource, bundled);
	}
	// A temp file, not a data: URL: see import-bundled-source.mjs.
	return importBundledSource(await bundled, 'rune-entry');
}

/**
 * The three passes, run ONCE per distinct entry source for the life of the
 * test process.
 *
 * Every loadRuneModule call still evaluates a FRESH module instance (one temp
 * file per import, see import-bundled-source.mjs), so per-test state isolation
 * is unchanged. Only the bundling is shared: it is a pure function of the entry
 * source and the on-disk tree, and the tree does not change while a test file
 * runs. Measured Tue 22 Sep 2026 on main 394e17f2: autoplay-stall-persistence
 * bundles its single entry 19 times, once per test, and the whole file sat at
 * the runner's 60 s per-file timeout on loaded agentbox hosts (jobs
 * 106269849983, 106613353127, 106743215758) with every test in it passing.
 */
async function _bundleRunes(entrySource) {
	const runes = await _bundle(entrySource, { stdin: true, deferSvelteRuntime: true });
	const compiled = compileModule(runes.outputFiles[0].text, {
		generate: 'client',
		filename: 'rune-entry.svelte.js'
	});
	const linked = await _bundle(compiled.js.code, { stdin: true });
	return linked.outputFiles[0].text;
}

/** Entry source -> promise of its bundled ESM text. See _bundleRunes. */
const _bundledBySource = new Map();

/** How many distinct entry sources this process has bundled so far. */
export function runeBundleCount() {
	return _bundledBySource.size;
}

/**
 * A browser-ish host for a rune module: localStorage that behaves, and interval
 * counters so a test can see whether a timer is actually running.
 *
 * Returns the live-interval reader plus a restore(). Real timers underneath -
 * the point is to observe the module's own scheduling, not to fake it.
 */
export function installTimerProbe() {
	const realSetInterval = globalThis.setInterval;
	const realClearInterval = globalThis.clearInterval;
	// setTimeout is left alone AND captured: flush() must not create work the
	// probe then counts, and a leaked interval would keep the test runner's
	// event loop open forever rather than failing.
	const realSetTimeout = globalThis.setTimeout;
	const realLocalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
	const store = new Map();
	let live = 0;

	globalThis.setInterval = (...args) => {
		live += 1;
		return realSetInterval(...args);
	};
	globalThis.clearInterval = (id) => {
		if (id !== null && id !== undefined) live -= 1;
		return realClearInterval(id);
	};
	Object.defineProperty(globalThis, 'localStorage', {
		configurable: true,
		value: {
			getItem: (key) => (store.has(key) ? store.get(key) : null),
			setItem: (key, value) => store.set(key, String(value)),
			removeItem: (key) => store.delete(key)
		}
	});

	return {
		liveIntervals: () => live,
		/** Let svelte's effect flush and any queued continuation settle. */
		flush: () => new Promise((resolve) => realSetTimeout(resolve, 40)),
		restore: () => {
			globalThis.setInterval = realSetInterval;
			globalThis.clearInterval = realClearInterval;
			if (realLocalStorage === undefined) delete globalThis.localStorage;
			else Object.defineProperty(globalThis, 'localStorage', realLocalStorage);
		}
	};
}
