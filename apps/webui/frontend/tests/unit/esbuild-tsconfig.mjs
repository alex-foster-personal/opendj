import * as esbuild from 'esbuild';

/**
 * The TypeScript options every test-side esbuild `build()` compiles with,
 * passed as `tsconfigRaw` so esbuild never reads tsconfig.json files.
 * Test code builds through `build` below, never esbuild's own.
 *
 * Why: without `tsconfigRaw`, each build reads tsconfig.json and the
 * `.svelte-kit/tsconfig.json` it extends. Test files that start a Vite server
 * with the SvelteKit plugin (`createServer` from 'vite', for example
 * automatic-rejoin.test.mjs) make SvelteKit rewrite `.svelte-kit/tsconfig.json`
 * as that server starts. Its writer caches previous contents per process, so a
 * fresh process always writes, and the write truncates the file first. A
 * build running at that moment in a concurrent test file reads an empty file
 * and fails with `.svelte-kit/tsconfig.json:1:0: ERROR: Unexpected end of
 * file in JSON`. That is how 8 tests in autoplay-error-hunt-report and
 * autoplay-explainer-placement failed on a loaded CI runner (run 38779491822,
 * job 116584650956, Thu 15 Oct 2026). The file is the same on main.
 *
 * Only the options esbuild acts on are here, with the values the tsconfig
 * chain sets (`target` decides the useDefineForClassFields default).
 * `$lib` resolution comes from each build's own `alias`, not tsconfig paths.
 * esbuild-tsconfig.test.mjs fails if these drift from the chain.
 */
export const TSCONFIG_RAW = Object.freeze({
	compilerOptions: Object.freeze({
		target: 'ES2022',
		strict: true,
		verbatimModuleSyntax: true
	})
});

/** esbuild's `build` with `tsconfigRaw` pinned to TSCONFIG_RAW. The one way
 * test code under tests/unit bundles; esbuild-tsconfig.test.mjs fails on any
 * other file there that imports anything but transform/transformSync from
 * 'esbuild'. A caller's own tsconfigRaw is refused, not silently replaced. */
export function build(options) {
	if (options.tsconfigRaw !== undefined) {
		throw new Error('esbuild-tsconfig.mjs build(): tsconfigRaw is pinned here; do not pass one');
	}
	return esbuild.build({ ...options, tsconfigRaw: TSCONFIG_RAW });
}
