/**
 * Client-compile and bundle real Svelte components for node:test click harnesses.
 * Not a *.test.mjs -- imported by preflight-run-setup.test.mjs (issue #2653).
 */
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { transform } from 'esbuild';

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

/** Stub unrelated imports while bundling a mount client for node:test. */
function testDependencyStubPlugin(dependencyContents) {
	return {
		name: 'test-dependency-stubs',
		setup(b) {
			b.onResolve({ filter: /.*/ }, ({ path }) => {
				const contents = dependencyContents(path);
				return contents !== undefined ? { path, namespace: 'test-dependency-stub' } : undefined;
			});
			b.onLoad({ filter: /.*/, namespace: 'test-dependency-stub' }, ({ path }) => {
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
	};
}

/** @param {{ nestedDependencyContents?: (path: string) => string | undefined }} [options] */
function svelteClientBundlePlugin(options = {}) {
	const { nestedDependencyContents } = options;
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
				const nestedPlugins = [viteUrlSuffixPlugin, recorderPickerTestStubsPlugin()];
				if (nestedDependencyContents) {
					nestedPlugins.unshift(testDependencyStubPlugin(nestedDependencyContents));
				}
				const typed = await build({
					absWorkingDir: FRONTEND_ROOT,
					alias: { $lib: LIB_ROOT },
					bundle: true,
					conditions: ['browser'],
					define: {
						'import.meta.env.DEV': 'false',
						'import.meta.env.VITE_API_BASE': JSON.stringify(
							'https://mount-svelte-client.example.test'
						)
					},
					entryPoints: [args.path],
					external: ['svelte', 'svelte/*'],
					format: 'esm',
					logLevel: 'silent',
					platform: 'browser',
					plugins: nestedPlugins,
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

/** Stand-ins for the performance modals' daemon calls and toast store; config answers one step, the rest refuse. */
const PERFORMANCE_MODAL_STUBS = new Map([
	[
		'$lib/rb/api-ingest',
		`const refuse = () => Promise.reject(new Error('performance modal stub: no daemon'));
export const getIngestConfig = () =>
	Promise.resolve({ path: '/stub', steps: [{ id: 'analysis', label: 'Analysis', hint: 'h', enabled: true, requires_rb_row: false }] });
export const putIngestConfig = refuse, startIngestRefresh = refuse, uploadIngestFiles = refuse, decideIngestUpload = refuse;`
	],
	[
		'$lib/rb/api-sample-helper',
		`export const samplePreviewSrc = () => null;
export const downloadSampleFromUrl = () => Promise.reject(new Error('performance modal stub: no daemon'));`
	],
	['$lib/rb/ingest-pending.svelte', 'export async function refreshIngestPending() {}'],
	['$lib/stores.svelte', 'export function pushToast() {}']
]);

/** Bundle the real performance-page SampleHelperModal and IngestDropModal for a client mount (issue #14604). */
export async function bundlePerformanceModalsClient() {
	const key = 'performance-modals';
	const cached = _bundles.get(key);
	if (cached) return cached;
	const stubs = {
		name: 'performance-modal-stubs',
		setup(b) {
			b.onResolve({ filter: /.*/ }, ({ path }) =>
				PERFORMANCE_MODAL_STUBS.has(path)
					? { path, namespace: 'performance-modal-stub' }
					: undefined
			);
			b.onLoad({ filter: /.*/, namespace: 'performance-modal-stub' }, ({ path }) => ({
				contents: PERFORMANCE_MODAL_STUBS.get(path),
				loader: 'js'
			}));
		}
	};
	const promise = (async () => {
		const result = await build({
			stdin: {
				contents: `export { mount, unmount, flushSync } from 'svelte';
export { default as SampleHelperModal } from '$lib/components/rb/SampleHelperModal.svelte';
export { default as IngestDropModal } from '$lib/components/rb/IngestDropModal.svelte';`,
				resolveDir: FRONTEND_ROOT,
				loader: 'js',
				sourcefile: 'performance-modals-client-entry.mjs'
			},
			absWorkingDir: FRONTEND_ROOT,
			alias: { $lib: LIB_ROOT },
			bundle: true,
			define: { 'import.meta.env.DEV': 'false' },
			format: 'esm',
			logLevel: 'silent',
			platform: 'browser',
			conditions: ['browser'],
			plugins: [viteUrlSuffixPlugin, stubs, svelteClientBundlePlugin()],
			target: 'es2022',
			write: false
		});
		if (!result.outputFiles[0]) throw new Error('performance modals client: empty bundle');
		return importBundledSource(result.outputFiles[0].text, 'performance-modals-client');
	})();
	_bundles.set(key, promise);
	return promise;
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

/**
 * Client-compile each `.svelte` component and `.svelte.ts` rune module on its own, leaving imports
 * to the outer bundle. svelteClientBundlePlugin bundles every rune module with its dependencies
 * first, which breaks a rune module that imports another rune module's `$state`.
 */
function svelteFileByFileClientPlugin() {
	return {
		name: 'svelte-file-by-file-client',
		setup(b) {
			b.onLoad({ filter: /\.svelte\.ts$/ }, async (args) => {
				const ts = await transform(readFileSync(args.path, 'utf8'), {
					loader: 'ts',
					sourcefile: args.path
				});
				const out = compileModule(ts.code, {
					generate: 'client',
					filename: args.path.replace(/\.ts$/, '.js')
				});
				return { contents: out.js.code, loader: 'js', resolveDir: dirname(args.path) };
			});
			b.onLoad({ filter: /\.svelte$/ }, (args) => {
				const out = compile(readFileSync(args.path, 'utf8'), {
					filename: args.path,
					generate: 'client',
					css: 'injected'
				});
				return { contents: out.js.code, loader: 'js', resolveDir: dirname(args.path) };
			});
		}
	};
}

/**
 * Bundle the four /performance banners and the stores that show them for a real client mount
 * (issue #14559). ReloadResumeBanner's own command dispatcher is a recorder, so its Resume click
 * is observable with no engine; every other importer gets the real performance IPC.
 */
export async function bundlePerformanceBannersClient() {
	const key = 'performance-banners';
	const cached = _bundles.get(key);
	if (cached) return cached;
	const ipcStub = {
		name: 'performance-banners-ipc-stub',
		setup(b) {
			const recorder = { path: 'dispatch-recorder', namespace: 'performance-banners-stub' };
			b.onResolve({ filter: /^\$lib\/rb\/performance-ipc\.svelte$/ }, ({ importer }) =>
				importer.endsWith('ReloadResumeBanner.svelte') ? recorder : undefined
			);
			b.onResolve({ filter: /^performance-banners-dispatch-recorder$/ }, () => recorder);
			b.onLoad({ filter: /.*/, namespace: 'performance-banners-stub' }, () => ({
				contents: `export const dispatchedCommands = [];
export async function dispatchPerformanceCommand(command) { dispatchedCommands.push(command); return {}; }`,
				loader: 'js'
			}));
		}
	};
	const promise = (async () => {
		const entry = `
export { mount, unmount, flushSync } from 'svelte';
export { default as ReloadResumeBanner } from '$lib/components/rb/ReloadResumeBanner.svelte';
export { default as AutoPlayStallBanner } from '$lib/components/rb/AutoPlayStallBanner.svelte';
export { default as ReplayTakeoverBar } from '$lib/components/rb/ReplayTakeoverBar.svelte';
export { default as TabLeaderBanner } from '$lib/components/rb/TabLeaderBanner.svelte';
export { reloadResume, offerReloadResume, dismissReloadResume } from '$lib/rb/reload-resume.svelte';
export { autoPlayStall, raiseAutoPlayStall, clearAutoPlayStall } from '$lib/rb/autoplay-stall.svelte';
export { tabLeadershipView, installTabLeadership } from '$lib/rb/tab-leadership.svelte';
export { dispatchedCommands } from 'performance-banners-dispatch-recorder';
export * as ids from '$lib/components/rb/banner-selector-ids';
export { snapshotSelectorNodes } from '$lib/keyboard/selector-register';
`;
		const result = await build({
			stdin: {
				contents: entry,
				resolveDir: FRONTEND_ROOT,
				loader: 'js',
				sourcefile: 'performance-banners-client-entry.mjs'
			},
			absWorkingDir: FRONTEND_ROOT,
			alias: { $lib: LIB_ROOT },
			bundle: true,
			define: {
				'import.meta.env.DEV': 'false',
				'import.meta.env.VITE_API_BASE': JSON.stringify('https://performance-banners.example.test')
			},
			format: 'esm',
			logLevel: 'silent',
			platform: 'browser',
			conditions: ['browser'],
			plugins: [viteUrlSuffixPlugin, ipcStub, svelteFileByFileClientPlugin()],
			target: 'es2022',
			write: false
		});
		if (!result.outputFiles[0]) throw new Error('performance banners client: empty bundle');
		return importBundledSource(result.outputFiles[0].text, 'performance-banners-client');
	})();
	_bundles.set(key, promise);
	return promise;
}

/** Bundle StemRow and its selector id helper for a real client mount (issue #14471). */
export async function bundleStemRowClient() {
	const key = 'stem-row';
	const cached = _bundles.get(key);
	if (cached) return cached;
	const promise = (async () => {
		const entry = `
import { mount, unmount, flushSync } from 'svelte';
export { default as StemRow } from '$lib/components/rb/deck/StemRow.svelte';
export * as ids from '$lib/components/rb/deck/deck-stem-selector-ids';
export { mount, unmount, flushSync };
`;
		const result = await build({
			stdin: {
				contents: entry,
				resolveDir: FRONTEND_ROOT,
				loader: 'js',
				sourcefile: 'stem-row-client-entry.mjs'
			},
			absWorkingDir: FRONTEND_ROOT,
			alias: { $lib: LIB_ROOT },
			bundle: true,
			define: {
				'import.meta.env.DEV': 'false',
				'import.meta.env.VITE_API_BASE': JSON.stringify('https://stem-row.example.test')
			},
			format: 'esm',
			logLevel: 'silent',
			platform: 'browser',
			conditions: ['browser'],
			plugins: [viteUrlSuffixPlugin, svelteClientBundlePlugin()],
			target: 'es2022',
			write: false
		});
		if (!result.outputFiles[0]) throw new Error('stem row client: empty bundle');
		return importBundledSource(result.outputFiles[0].text, 'stem-row-client');
	})();
	_bundles.set(key, promise);
	return promise;
}

/** Bundle RecentlyDeletedFolderView for client mount and row-refresh harness (#13326). */
export async function bundleRecentlyDeletedFolderViewClient() {
	const key = 'recently-deleted-folder-view';
	const cached = _bundles.get(key);
	if (cached) return cached;
	const promise = (async () => {
		const entry = `
import { mount, unmount, flushSync } from 'svelte';
export { default as RecentlyDeletedFolderView } from '$lib/components/rb/browser/RecentlyDeletedFolderView.svelte';
export { default as RecentlyDeletedFolderHarness } from './tests/unit/fixtures/recently-deleted-folder-view-harness.svelte';
export { mount, unmount, flushSync };
`;
		const result = await build({
			stdin: {
				contents: entry,
				resolveDir: FRONTEND_ROOT,
				loader: 'js',
				sourcefile: 'recently-deleted-folder-view-client-entry.mjs'
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
		if (!result.outputFiles[0])
			throw new Error('recently deleted folder view client: empty bundle');
		return importBundledSource(result.outputFiles[0].text, 'recently-deleted-folder-view-client');
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
			testDependencyStubPlugin(dependencyContents),
			svelteClientBundlePlugin({ nestedDependencyContents: dependencyContents })
		],
		target: 'es2022',
		write: false
	});
	if (!result.outputFiles[0]) throw new Error('recorder retry client: empty bundle');
	return importBundledSource(result.outputFiles[0].text, 'recorder-status-retry-client');
}

/** Bundle UsbSourceListView for a real client mount (issue #13407). */
export async function bundleUsbSourceListViewClient() {
	const key = 'usb-source-list-view';
	const cached = _bundles.get(key);
	if (cached) return cached;
	const promise = (async () => {
		const entry = `
import { mount, unmount, flushSync } from 'svelte';
export { default as UsbSourceListView } from '$lib/components/rb/browser/UsbSourceListView.svelte';
export { mount, unmount, flushSync };
`;
		const result = await build({
			stdin: {
				contents: entry,
				resolveDir: FRONTEND_ROOT,
				loader: 'js',
				sourcefile: 'usb-source-list-view-client-entry.mjs'
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
		if (!result.outputFiles[0]) throw new Error('usb source list view client: empty bundle');
		return importBundledSource(result.outputFiles[0].text, 'usb-source-list-view-client');
	})();
	_bundles.set(key, promise);
	return promise;
}

/** Bundle OjStarterChips for a real client mount (issue #14040). */
export async function bundleOjStarterChipsClient() {
	const key = 'oj-starter-chips';
	const cached = _bundles.get(key);
	if (cached) return cached;
	const promise = (async () => {
		const entry = `
import { mount, unmount, flushSync } from 'svelte';
export { default as OjStarterChips } from '$lib/components/assistant/OjStarterChips.svelte';
export { mount, unmount, flushSync };
`;
		const result = await build({
			stdin: {
				contents: entry,
				resolveDir: FRONTEND_ROOT,
				loader: 'js',
				sourcefile: 'oj-starter-chips-client-entry.mjs'
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
		if (!result.outputFiles[0]) throw new Error('oj starter chips client: empty bundle');
		return importBundledSource(result.outputFiles[0].text, 'oj-starter-chips-client');
	})();
	_bundles.set(key, promise);
	return promise;
}

/**
 * Per-file client compile: each `.svelte.ts` rune module is transformed and
 * compiled on its own (never pre-bundled), so a rune module that imports
 * another, or `svelte` itself, keeps one shared instance in the outer bundle.
 */
const perFileSvelteClientPlugin = {
	name: 'svelte-client-per-file',
	setup(b) {
		b.onLoad({ filter: /\.svelte\.(ts|js)$/ }, async (args) => {
			const source = readFileSync(args.path, 'utf8');
			const js = args.path.endsWith('.ts')
				? (await transform(source, { loader: 'ts', sourcefile: args.path })).code
				: source;
			const out = compileModule(js, { generate: 'client', filename: args.path });
			return { contents: out.js.code, loader: 'js', resolveDir: dirname(args.path) };
		});
		b.onLoad({ filter: /\.svelte$/ }, (args) => {
			const source = readFileSync(args.path, 'utf8');
			const out = compile(source, { filename: args.path, generate: 'client', css: 'injected' });
			return { contents: out.js.code, loader: 'js', resolveDir: dirname(args.path) };
		});
	}
};

/** Bundle HeadphoneCluster with the mixer read model and I/O surface it reads (issue #12692). */
export async function bundleHeadphoneClusterClient() {
	const key = 'headphone-cluster';
	const cached = _bundles.get(key);
	if (cached) return cached;
	const promise = (async () => {
		const entry = `
import { mount, unmount, flushSync } from 'svelte';
export { default as HeadphoneCluster } from '$lib/components/rb/mixer/HeadphoneCluster.svelte';
export { mixerState } from '$lib/player/state.svelte';
export { openIoView, closeIoView } from '$lib/rb/io-surface.svelte';
export { mount, unmount, flushSync };
`;
		const result = await build({
			stdin: {
				contents: entry,
				resolveDir: FRONTEND_ROOT,
				loader: 'js',
				sourcefile: 'headphone-cluster-client-entry.mjs'
			},
			absWorkingDir: FRONTEND_ROOT,
			alias: { $lib: LIB_ROOT },
			bundle: true,
			define: {
				'import.meta.env.DEV': 'false',
				'import.meta.env.VITE_API_BASE': JSON.stringify('https://headphone-cluster.example.test')
			},
			format: 'esm',
			loader: { '.css': 'empty' },
			logLevel: 'silent',
			platform: 'browser',
			conditions: ['browser'],
			plugins: [viteUrlSuffixPlugin, perFileSvelteClientPlugin],
			resolveExtensions: ['.svelte', '.ts', '.js', '.mjs', '.json'],
			target: 'es2022',
			write: false
		});
		if (!result.outputFiles[0]) throw new Error('headphone cluster client: empty bundle');
		return importBundledSource(result.outputFiles[0].text, 'headphone-cluster-client');
	})();
	_bundles.set(key, promise);
	return promise;
}

/**
 * Stand-ins for the popover's write path, Settings opener, previews, the glyph's daemon flag and its selector node: the real
 * apply.ts graph pulls audio-engine rune modules this bundler cannot compile. Reads answer catalog defaults.
 */
const WAVE_QS_STUBS = new Map([
	[
		'$lib/settings/apply',
		`import { SETTINGS_CATALOG } from '$lib/settings/catalog';
export function isAllowedSettingKey() { return true; }
export function applySettingChange() {}
export function readSettingValue(id) {
	const def = SETTINGS_CATALOG.find((d) => d.id === id);
	if (def === undefined) throw new Error('wave qs stub: unknown setting ' + id);
	if (def.control.kind === 'boolean') return true;
	if (def.control.kind === 'enum') return def.control.options[0].value;
	if (def.control.kind === 'number') return String(def.control.min);
	throw new Error('wave qs stub: no read for ' + def.control.kind);
}`
	],
	['$lib/settings/hotkeys', 'export function openSettings() {}'],
	['$lib/rb/waveform-quick-settings-flag', 'export function waveQsEnabled() { return true; }'],
	['$lib/components/settings/SettingPreview.svelte', '<div class="wqs-preview-stub"></div>'],
	// The glyph's keyboard-selector registration (#13700) needs ResizeObserver, which the DOM shim lacks;
	// the selector has its own tests, so here it is an inert action.
	[
		'./topbar-selector-node',
		'export function topbarSelectorNode() { return { update() {}, destroy() {} }; }'
	]
]);

/** Bundle the real waveform quick-settings glyph and its lazily imported popover for a client mount (issue #12520). */
export async function bundleWaveQuickSettingsClient() {
	const key = 'wave-quick-settings';
	const cached = _bundles.get(key);
	if (cached) return cached;
	const stubFor = (path) => WAVE_QS_STUBS.get(path);
	const stubs = {
		name: 'wave-qs-stubs',
		setup(b) {
			b.onResolve({ filter: /.*/ }, ({ path }) =>
				stubFor(path) !== undefined ? { path, namespace: 'wave-qs-stub' } : undefined
			);
			b.onLoad({ filter: /.*/, namespace: 'wave-qs-stub' }, ({ path }) => {
				const contents = stubFor(path);
				return {
					contents: path.endsWith('.svelte')
						? compile(contents, { generate: 'client' }).js.code
						: contents,
					loader: 'js',
					resolveDir: FRONTEND_ROOT
				};
			});
		}
	};
	const promise = (async () => {
		const entry = `
export { mount, unmount, flushSync, tick } from 'svelte';
export { default as WaveQuickSettingsButton } from '$lib/components/rb/WaveQuickSettingsButton.svelte';
export { default as WaveQuickSettingsPopover } from '$lib/components/rb/WaveQuickSettingsPopover.svelte';
`;
		const result = await build({
			stdin: {
				contents: entry,
				resolveDir: FRONTEND_ROOT,
				loader: 'js',
				sourcefile: 'wave-quick-settings-client-entry.mjs'
			},
			absWorkingDir: FRONTEND_ROOT,
			alias: { $lib: LIB_ROOT },
			bundle: true,
			define: {
				'import.meta.env.DEV': 'false',
				'import.meta.env.VITE_API_BASE': JSON.stringify('https://wave-qs.example.test')
			},
			format: 'esm',
			logLevel: 'silent',
			platform: 'browser',
			conditions: ['browser'],
			plugins: [viteUrlSuffixPlugin, stubs, svelteClientBundlePlugin()],
			target: 'es2022',
			write: false
		});
		if (!result.outputFiles[0]) throw new Error('wave quick settings client: empty bundle');
		return importBundledSource(result.outputFiles[0].text, 'wave-quick-settings-client');
	})();
	_bundles.set(key, promise);
	return promise;
}
