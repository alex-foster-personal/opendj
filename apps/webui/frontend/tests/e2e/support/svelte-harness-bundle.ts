/**
 * Bundle a tiny Svelte harness around REAL src/lib components for a Chromium
 * component test (page.setContent + addScriptTag, no dev server).
 *
 * Every .svelte file is compiled with Svelte's own client compiler and CSS is
 * injected, so component styles apply exactly as they ship. Nothing is stubbed.
 */
import { appendFileSync, mkdirSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
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
		// The bundle's inputs are the spec's footprint (recordHarnessFootprint below).
		metafile: true,
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
	recordHarnessFootprint(Object.keys(result.metafile.inputs));
	return result.outputFiles[0].text;
}

/**
 * Read a `src/` file a harness spec injects by hand (a stylesheet it hands to
 * `page.addStyleTag`, say). Neither the dev-server request log nor the bundle's
 * metafile sees a plain `readFile`, so this records the path as part of the
 * spec's footprint the same way the bundle's inputs are (Codex, PR #7948).
 * `path` is relative to the frontend root and starts with `src/`.
 */
export async function readSrcForHarness(path: string): Promise<string> {
	if (!path.startsWith('src/')) {
		throw new Error(`readSrcForHarness: ${path} is not under src/`);
	}
	const text = await readFile(join(FRONTEND_ROOT, path), 'utf8');
	recordHarnessFootprint([path]);
	return text;
}

/**
 * A harness bundle never fetches its sources through Vite, so the dev-server
 * request log (`vite-e2e-footprints.ts`) sees nothing of the component under
 * test. While `E2E_FOOTPRINTS_DIR` is set, the bundle's `src/` inputs are
 * appended to the same `requests.jsonl` with this worker's clock, which is the
 * clock the footprint reporter stamps the spec with, so the map builder lands
 * them in this spec's window (Codex, PR #7948).
 */
function recordHarnessFootprint(inputs: string[]): void {
	const dir = process.env.E2E_FOOTPRINTS_DIR;
	if (!dir) return;
	mkdirSync(dir, { recursive: true });
	const now = Date.now();
	const lines = inputs
		.map((input) => input.split('?', 1)[0].split('\\').join('/'))
		.filter((input) => input.startsWith('src/'))
		.map((path) => JSON.stringify({ t: now, path }) + '\n')
		.join('');
	if (lines) appendFileSync(join(dir, 'requests.jsonl'), lines);
}
