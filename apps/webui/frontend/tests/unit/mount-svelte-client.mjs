/**
 * Client-compile and bundle real Svelte components for node:test click harnesses.
 * Not a *.test.mjs — imported by preflight-run-setup.test.mjs (issue #2653).
 */
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { build } from './esbuild-tsconfig.mjs';
import { compile, compileModule } from 'svelte/compiler';

import { importBundledSource } from './import-bundled-source.mjs';
import { viteUrlSuffixPlugin } from './vite-url-suffix-plugin.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const LIB_ROOT = fileURLToPath(new URL('../../src/lib', import.meta.url));

/** @type {Map<string, Promise<Record<string, unknown>>>} */
const _bundles = new Map();

function svelteClientBundlePlugin() {
	return {
		name: 'mount-svelte-client',
		setup(b) {
			b.onResolve({ filter: /\.svelte$/ }, (args) => {
				const base = args.path.startsWith('$lib/')
					? join(LIB_ROOT, args.path.slice('$lib/'.length))
					: resolve(args.resolveDir, args.path);
				if (existsSync(`${base}.ts`) && !existsSync(base)) {
					return { path: `${base}.ts`, namespace: 'svelte-rune-client' };
				}
				return undefined;
			});
			b.onResolve({ filter: /\.svelte\.ts$/ }, (args) => ({
				path: args.path,
				namespace: 'svelte-rune-client'
			}));
			b.onLoad({ filter: /\.ts$/, namespace: 'svelte-rune-client' }, async (args) => {
				const source = readFileSync(args.path, 'utf8');
				const typed = await build({
					stdin: { contents: source, loader: 'ts', resolveDir: dirname(args.path) },
					absWorkingDir: FRONTEND_ROOT,
					alias: { $lib: LIB_ROOT },
					bundle: true,
					define: {
						'import.meta.env.DEV': 'false',
						'import.meta.env.VITE_API_BASE': JSON.stringify(
							'https://mount-svelte-client.example.test'
						)
					},
					format: 'esm',
					logLevel: 'silent',
					platform: 'browser',
					plugins: [viteUrlSuffixPlugin],
					target: 'es2022',
					write: false
				});
				const out = compileModule(typed.outputFiles[0].text, {
					generate: 'client',
					filename: args.path.replace(/\.ts$/, '.svelte.js')
				});
				return { contents: out.js.code, loader: 'js', resolveDir: dirname(args.path) };
			});
			b.onLoad({ filter: /\.svelte$/ }, (args) => {
				const source = readFileSync(args.path, 'utf8');
				const out = compile(source, { filename: args.path, generate: 'client', css: 'injected' });
				return { contents: out.js.code, loader: 'js', resolveDir: dirname(args.path) };
			});
		}
	};
}

/**
 * Bundle PreflightCheckRow for a real client mount (issue #2653).
 * @param {string} [viteApiBase]
 */
export async function bundlePreflightCheckRowClient(
	viteApiBase = 'https://preflight-run-setup.example.test'
) {
	const cached = _bundles.get(viteApiBase);
	if (cached) return cached;

	const promise = (async () => {
		const entry = `
import { mount, unmount, flushSync } from 'svelte';
export { default as PreflightCheckRow } from '$lib/components/preflight/PreflightCheckRow.svelte';
export { mount, unmount, flushSync };
`;
		const result = await build({
			stdin: {
				contents: entry,
				resolveDir: FRONTEND_ROOT,
				loader: 'js',
				sourcefile: 'preflight-check-row-client-entry.mjs'
			},
			absWorkingDir: FRONTEND_ROOT,
			alias: { $lib: LIB_ROOT },
			bundle: true,
			define: {
				'import.meta.env.DEV': 'false',
				'import.meta.env.VITE_API_BASE': JSON.stringify(viteApiBase)
			},
			format: 'esm',
			logLevel: 'silent',
			platform: 'browser',
			conditions: ['browser'],
			plugins: [viteUrlSuffixPlugin, svelteClientBundlePlugin()],
			target: 'es2022',
			write: false
		});
		if (!result.outputFiles[0]) throw new Error('preflight check row client: empty bundle');
		return importBundledSource(result.outputFiles[0].text, 'preflight-check-row-client');
	})();

	_bundles.set(viteApiBase, promise);
	return promise;
}

/** Bundle StemWaveRowView for a real client mount (issue #10317). */
export async function bundleStemWaveRowViewClient() {
	const key = 'stem-wave-row-view';
	const cached = _bundles.get(key);
	if (cached) return cached;
	const promise = (async () => {
		const entry = `
import { mount, unmount, flushSync } from 'svelte';
export { default as StemWaveRowView } from '$lib/components/rb/wave/StemWaveRowView.svelte';
export { mount, unmount, flushSync };
`;
		const result = await build({
			stdin: {
				contents: entry,
				resolveDir: FRONTEND_ROOT,
				loader: 'js',
				sourcefile: 'stem-wave-row-view-client-entry.mjs'
			},
			absWorkingDir: FRONTEND_ROOT,
			alias: { $lib: LIB_ROOT },
			bundle: true,
			define: { 'import.meta.env.DEV': 'false' },
			format: 'esm',
			logLevel: 'silent',
			platform: 'browser',
			conditions: ['browser'],
			plugins: [viteUrlSuffixPlugin, svelteClientBundlePlugin()],
			target: 'es2022',
			write: false
		});
		if (!result.outputFiles[0]) throw new Error('stem wave row view client: empty bundle');
		return importBundledSource(result.outputFiles[0].text, 'stem-wave-row-view-client');
	})();
	_bundles.set(key, promise);
	return promise;
}
