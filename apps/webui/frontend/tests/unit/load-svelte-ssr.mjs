/**
 * Render a real Svelte COMPONENT in this suite, without a browser.
 *
 * Added Thu 10 Sep 2026 (Codex r3973806306 on PR #1654): every component check
 * in tests/unit until now was a regex over `.svelte` source, which cannot
 * establish that a component mounts, that its `{#if}` gates on the state it
 * claims to, or that it renders the text it claims to. Those are runtime
 * questions and they now get a runtime answer.
 *
 * WHAT THIS CAN AND CANNOT PROVE, stated up front so nobody reads more into a
 * pass than is there. It compiles the component with `generate: 'server'` and
 * runs svelte's own SSR renderer, so it PROVES the component executes, that
 * its template branches on real store state, and what markup it produces for a
 * given state. It CANNOT prove reactivity to a LATER state change, event
 * handlers, or anything about layout, CSS or stacking - there is no DOM here
 * (no jsdom/happy-dom in this project, deliberately: the unit suite's whole
 * dependency surface is esbuild plus svelte). A claim about what the operator
 * SEES on screen still needs a browser test.
 *
 * Same three-pass shape as load-rune-module.mjs, with the compiler pointed at
 * the server generator: bundle with runes intact, compile, bundle again to
 * inline `svelte/internal/server`, import from a data URL.
 */
import { readFile } from 'node:fs/promises';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

import { build, transform } from 'esbuild';
import { compile, compileModule } from 'svelte/compiler';

import { importBundledSource } from './import-bundled-source.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const LIB_ROOT = fileURLToPath(new URL('../../src/lib', import.meta.url));

/** Compile `.svelte` components and `.svelte.ts` rune modules for the server. */
const sveltePlugin = {
	name: 'svelte-ssr',
	setup(builder) {
		// Ordered before the component rule by specificity, not by luck: the
		// two filters are disjoint (`.svelte.ts` never matches `\.svelte$`).
		builder.onLoad({ filter: /\.svelte\.(ts|js)$/ }, async (args) => {
			const source = await readFile(args.path, 'utf8');
			const js = args.path.endsWith('.ts')
				? (await transform(source, { loader: 'ts', sourcefile: args.path })).code
				: source;
			const compiled = compileModule(js, { generate: 'server', filename: args.path });
			return { contents: compiled.js.code, loader: 'js', resolveDir: dirname(args.path) };
		});
		builder.onLoad({ filter: /\.svelte$/ }, async (args) => {
			const source = await readFile(args.path, 'utf8');
			const compiled = compile(source, { generate: 'server', filename: args.path });
			return { contents: compiled.js.code, loader: 'js', resolveDir: dirname(args.path) };
		});
	}
};

/**
 * Bundle and import `entrySource`, TypeScript evaluated at the frontend root.
 *
 * Re-export the component under test plus whatever module state the test needs
 * to set before rendering, exactly as loadRuneModule's entry does.
 */
export async function loadSvelteSsrModule(entrySource) {
	const bundled = await build({
		stdin: {
			contents: entrySource,
			resolveDir: FRONTEND_ROOT,
			loader: 'ts',
			sourcefile: 'ssr-entry.ts'
		},
		absWorkingDir: FRONTEND_ROOT,
		alias: { $lib: LIB_ROOT },
		bundle: true,
		define: { 'import.meta.env.VITE_API_BASE': JSON.stringify('https://ssr-harness.example.test') },
		format: 'esm',
		logLevel: 'silent',
		platform: 'node',
		plugins: [sveltePlugin],
		resolveExtensions: ['.svelte', '.ts', '.js', '.mjs', '.json'],
		target: 'es2022',
		write: false
	});
	// A temp file, not a data: URL: see import-bundled-source.mjs.
	return importBundledSource(bundled.outputFiles[0].text, 'svelte-ssr-entry');
}
