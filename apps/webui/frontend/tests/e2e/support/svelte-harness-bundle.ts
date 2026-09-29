/**
 * Bundle a tiny Svelte harness around REAL src/lib components for a Chromium
 * component test (page.setContent + addScriptTag, no dev server).
 *
 * Every .svelte file is compiled with Svelte's own client compiler and CSS is
 * injected, so component styles apply exactly as they ship. Nothing is stubbed.
 */
import { readFile } from 'node:fs/promises';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

import { build, transform } from 'esbuild';
import { compile, compileModule } from 'svelte/compiler';

const FRONTEND_ROOT = fileURLToPath(new URL('../../..', import.meta.url));
const LIB_ROOT = fileURLToPath(new URL('../../../src/lib', import.meta.url));

export async function bundleSvelteHarness(harness: string): Promise<string> {
	const result = await build({
		stdin: {
			contents:
				"import { mount } from 'svelte';\nimport Harness from 'virtual:harness.svelte';\nmount(Harness, { target: document.body });",
			resolveDir: FRONTEND_ROOT,
			loader: 'js'
		},
		absWorkingDir: FRONTEND_ROOT,
		alias: { $lib: LIB_ROOT },
		bundle: true,
		conditions: ['browser'],
		// src/lib/api/base.ts reads VITE_API_BASE; unset means "same origin",
		// which is what the app gets without the variable too.
		define: { 'import.meta.env': '{}' },
		format: 'iife',
		logLevel: 'silent',
		platform: 'browser',
		write: false,
		plugins: [
			{
				name: 'svelte-client',
				setup(b) {
					// Vite's `?url` import yields the asset's URL string. Audio worklet
					// modules are imported this way; a harness never registers them,
					// so the URL only has to be the string Vite would hand back.
					b.onResolve({ filter: /\?url$/ }, (args) => ({
						path: args.path.replace(/^\$lib\//, '/src/lib/').replace(/\?url$/, ''),
						namespace: 'asset-url'
					}));
					b.onLoad({ filter: /.*/, namespace: 'asset-url' }, (args) => ({
						contents: `export default ${JSON.stringify(args.path)};`,
						loader: 'js'
					}));
					b.onResolve({ filter: /^virtual:harness\.svelte$/ }, () => ({
						path: 'harness.svelte',
						namespace: 'harness'
					}));
					b.onLoad({ filter: /.*/, namespace: 'harness' }, () => ({
						contents: compile(harness, { filename: 'Harness.svelte', generate: 'client' }).js.code,
						loader: 'js',
						resolveDir: FRONTEND_ROOT
					}));
					// Rune modules (*.svelte.ts / *.svelte.js) go through compileModule,
					// after esbuild strips their TypeScript, exactly as the Svelte plugin
					// does in the app build.
					b.onLoad({ filter: /\.svelte\.(ts|js)$/ }, async (args) => {
						const source = await readFile(args.path, 'utf8');
						const js = args.path.endsWith('.ts')
							? (await transform(source, { loader: 'ts', format: 'esm' })).code
							: source;
						const out = compileModule(js, { filename: args.path, generate: 'client' });
						return { contents: out.js.code, loader: 'js', resolveDir: dirname(args.path) };
					});
					b.onLoad({ filter: /\.svelte$/ }, async (args) => {
						const source = await readFile(args.path, 'utf8');
						const out = compile(source, { filename: args.path, generate: 'client', css: 'injected' });
						return { contents: out.js.code, loader: 'js', resolveDir: dirname(args.path) };
					});
				}
			}
		]
	});
	return result.outputFiles[0].text;
}
