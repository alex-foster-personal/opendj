import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { build } from 'esbuild';
import { compile } from 'svelte/compiler';

import { importBundledSource } from './import-bundled-source.mjs';
import { viteUrlSuffixPlugin } from './vite-url-suffix-plugin.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const LIB_ROOT = fileURLToPath(new URL('../../src/lib', import.meta.url));

/**
 * Compile every `.svelte` component through the REAL Svelte compiler
 * (`generate: 'server'`), instead of the `$state`-as-identity stub
 * `load-typescript.mjs` uses for plain `.svelte.ts` store modules.
 *
 * A `.svelte.ts` module has no template; its runes are just typed containers
 * and identity-stubbing them is an honest stand-in (see load-typescript.mjs).
 * A `.svelte` FILE has markup and `{#if}` branches driven by those runes, and
 * that logic is exactly what SAND-01 review round 3 found untested: a test
 * that greps the component's raw source for a string proves the string is in
 * the file, never that Svelte mounts the section, updates on the reactive
 * state, or renders the branch a user would actually see. Running the real
 * compiler and Svelte's own SSR renderer against it answers that for real.
 */
const svelteComponentPlugin = {
	name: 'svelte-server-compile',
	setup(pluginBuild) {
		pluginBuild.onLoad({ filter: /\.svelte$/ }, (args) => {
			const source = readFileSync(args.path, 'utf8');
			const result = compile(source, { generate: 'server', filename: args.path });
			return { contents: result.js.code, loader: 'js', resolveDir: dirname(args.path) };
		});
	}
};

/**
 * Bundle a small ESM entry (a handful of `export { x } from '$lib/...'`
 * lines) into one module, so a `.svelte` component and the `.svelte.ts`
 * stores it reads share ONE instance of each store rather than one copy per
 * bundle - the same reason `openAccountOverlay()` must live in the same
 * bundle as the component that reads `accountOverlay.open`.
 */
export async function bundleSvelteEntry(entrySource) {
	const result = await build({
		stdin: { contents: entrySource, resolveDir: FRONTEND_ROOT, loader: 'ts' },
		absWorkingDir: FRONTEND_ROOT,
		alias: { $lib: LIB_ROOT },
		bundle: true,
		define: {
			$state: 'globalThis.__musicDjToolsTestState',
			'import.meta.env.VITE_API_BASE': JSON.stringify('https://store-build.example.test')
		},
		format: 'esm',
		logLevel: 'silent',
		platform: 'node',
		plugins: [svelteComponentPlugin, viteUrlSuffixPlugin],
		target: 'node20',
		write: false
	});
	if (result.outputFiles.length !== 1) {
		throw new Error(`expected one bundled output, got ${result.outputFiles.length}`);
	}
	// See load-typescript.mjs for why $state is an identity function under
	// test: runes are compile-time, and a plain structured value is an honest
	// stand-in for the deep-clone the real rune performs.
	globalThis.__musicDjToolsTestState = (value) => value;
	globalThis.__musicDjToolsTestState.snapshot = (value) =>
		value === undefined ? undefined : JSON.parse(JSON.stringify(value));
	// A temp file, not a data: URL: see import-bundled-source.mjs.
	return importBundledSource(result.outputFiles[0].text, 'svelte-entry');
}

/** Render a real Svelte SSR component to its HTML body string. */
export async function renderToHtml(component, props = {}) {
	const { render } = await import('svelte/server');
	return render(component, { props }).body;
}
