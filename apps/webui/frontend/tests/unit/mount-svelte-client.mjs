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

/** Map a resolved absolute lib path back to the $lib/ import key used in dependency stubs. */
function libImportKey(path) {
	if (path.startsWith('$lib/')) return path;
	if (path.startsWith(LIB_ROOT)) {
		const tail = path.slice(LIB_ROOT.length).replace(/^\//, '');
		return tail ? `$lib/${tail}` : null;
	}
	return null;
}

/** @type {Map<string, Promise<Record<string, unknown>>>} */
const _bundles = new Map();

/** Stub recorder picker load for nested esbuild passes inside client bundles. */
function recorderPickerTestStubsPlugin() {
	return {
		name: 'recorder-picker-test-stubs',
		setup(b) {
			b.onResolve({ filter: /record-input-picker-loader/ }, () => ({
				path: 'record-input-picker-loader-stub',
				namespace: 'recorder-picker-test-stub'
			}));
			b.onResolve({ filter: /RecordInputPicker\.svelte$/ }, (args) => ({
				path: args.path,
				namespace: 'recorder-picker-test-stub'
			}));
			b.onLoad({ filter: /.*/, namespace: 'recorder-picker-test-stub' }, (args) => {
				const contents = args.path.includes('loader')
					? 'export async function loadRecordInputPicker() { return null; }'
					: 'export default function RecordInputPicker() { return null; }';
				return { contents, loader: 'js' };
			});
		}
	};
}

function svelteClientBundlePlugin() {
	return {
		name: 'mount-svelte-client',
		setup(b) {
			b.onResolve({ filter: /\.svelte$/ }, (args) => {
				const base = args.path.startsWith('$fixtures/')
					? join(FRONTEND_ROOT, 'tests/unit/fixtures', args.path.slice('$fixtures/'.length))
					: args.path.startsWith('$lib/')
						? join(LIB_ROOT, args.path.slice('$lib/'.length))
						: resolve(args.resolveDir, args.path);
				if (existsSync(`${base}.ts`) && !existsSync(base)) {
					return { path: `${base}.ts`, namespace: 'svelte-rune-client' };
				}
				return undefined;
			});
			b.onResolve({ filter: /\.svelte\.ts$/ }, (args) => {
				const path = args.path.startsWith('$fixtures/')
					? join(FRONTEND_ROOT, 'tests/unit/fixtures', args.path.slice('$fixtures/'.length))
					: args.path.startsWith('$lib/')
						? join(LIB_ROOT, args.path.slice('$lib/'.length))
						: args.path;
				return { path, namespace: 'svelte-rune-client' };
			});
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
					plugins: [viteUrlSuffixPlugin, recorderPickerTestStubsPlugin()],
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

/** Bundle the real REC rail and effects, isolating unrelated chrome/audio dependencies. */
export async function bundleRecorderStatusRetryClient() {
	const dependencies = new Map([
		[
			'$lib/stores.svelte',
			`export const toastCalls = [];
export function pushToast(...args) { toastCalls.push(args); }`
		],
		[
			'$lib/rb/playlist-history-chrome.svelte',
			`export function showWholeCollectionFromChrome() {
throw new Error('unexpected navigation'); }`
		],
		[
			'$lib/rb/usb-export.svelte',
			`export function openUsbExport() {
throw new Error('unexpected USB export'); }`
		],
		['$lib/sets/recording-flag', 'export function setRecordingEnabled() { return true; }'],
		[
			'$lib/sets/record-input-picker-loader.ts',
			`export async function loadRecordInputPicker() { return null; }
export type RecordInputPickerComponent = null;`
		],
		[
			'$lib/sets/master-mix-capture',
			`export class MasterMixCancelled extends Error {}
export function ensureMasterMixCapture() { throw new Error('unexpected master capture'); }
export function stopMasterMixCapture() { throw new Error('unexpected master stop'); }`
		],
		['./IconRail.svelte', '<div></div>'],
		['./RecordInputPicker.svelte', '<div></div>'],
		['$lib/components/rb/browser/RecordInputPicker.svelte', '<div></div>']
	]);
	const dependencyContents = (path) => {
		const keys = [path, libImportKey(path)].filter(Boolean);
		for (const key of keys) {
			if (dependencies.has(key)) return dependencies.get(key);
		}
		if (path.endsWith('RecordInputPicker.svelte')) return '<div></div>';
		return undefined;
	};
	const result = await build({
		stdin: {
			contents: `export { mount, unmount, flushSync } from 'svelte';
export { default as RecorderHarness } from './tests/unit/fixtures/recorder-status-retry-harness.svelte';
export { toastCalls } from '$lib/stores.svelte';`,
			resolveDir: FRONTEND_ROOT,
			loader: 'js'
		},
		absWorkingDir: FRONTEND_ROOT,
		alias: { $lib: LIB_ROOT },
		bundle: true,
		define: {
			'import.meta.env.DEV': 'false',
			'import.meta.env.VITE_API_BASE': JSON.stringify('https://recorder-retry.example.test')
		},
		format: 'esm',
		logLevel: 'silent',
		platform: 'browser',
		conditions: ['browser'],
		plugins: [
			recorderPickerTestStubsPlugin(),
			{
				name: 'recorder-unrelated-dependencies',
				setup(b) {
					b.onResolve({ filter: /.*/ }, ({ path }) => {
						const contents = dependencyContents(path);
						return contents !== undefined ? { path, namespace: 'recorder-dependency' } : undefined;
					});
					b.onLoad({ filter: /.*/, namespace: 'recorder-dependency' }, ({ path }) => {
						const contents = dependencyContents(path);
						return {
							contents:
								contents?.startsWith('<') && path.endsWith('.svelte')
									? compile(contents, { generate: 'client' }).js.code
									: contents,
							loader: 'js',
							resolveDir: FRONTEND_ROOT
						};
					});
				}
			},
			svelteClientBundlePlugin()
		],
		target: 'es2022',
		write: false
	});
	if (!result.outputFiles[0]) throw new Error('recorder retry client: empty bundle');
	return importBundledSource(result.outputFiles[0].text, 'recorder-status-retry-client');
}
