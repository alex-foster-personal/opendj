import { existsSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { build } from 'esbuild';

import { importBundledSource } from './import-bundled-source.mjs';
import { viteUrlSuffixPlugin } from './vite-url-suffix-plugin.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const LIB_ROOT = fileURLToPath(new URL('../../src/lib', import.meta.url));

/**
 * Svelte components, modelled for the test bundler.
 *
 * A module under test may import a `.svelte` file it mounts on demand (the
 * telemetry consent dialog is imported and mounted from inside a deferred
 * boot task, so the component never joins the first-paint bundle). esbuild
 * has no Svelte loader and would refuse the whole bundle. Node tests never
 * render, so the component resolves to a stub whose default export FAILS
 * LOUD if anything tries to mount it: a test that reaches the real
 * component has reached the DOM, and that is a browser test's job.
 */
const svelteComponentStubs = {
	name: 'svelte-component-stub',
	setup(build) {
		build.onResolve({ filter: /\.svelte$/ }, (args) => {
			// `./foo.svelte` is ALSO how a `foo.svelte.ts` rune module is imported
			// (extensionless). Only a real component file is stubbed; a rune
			// module resolves as the TypeScript it is.
			const base = args.path.startsWith('$lib/')
				? join(LIB_ROOT, args.path.slice('$lib/'.length))
				: resolve(args.resolveDir, args.path);
			if (existsSync(`${base}.ts`) || !existsSync(base)) return undefined;
			return { path: args.path, namespace: 'svelte-component-stub' };
		});
		build.onLoad({ filter: /.*/, namespace: 'svelte-component-stub' }, (args) => ({
			contents:
				`export default function SvelteComponentStub() { throw new Error(${JSON.stringify(
					`${args.path} is a Svelte component stub under node tests; mounting it needs a browser`
				)}); }`,
			loader: 'js'
		}));
	}
};

/**
 * Bundle one frontend TypeScript module to ESM source text, without
 * importing it. Exists so a caller that needs the SAME bundle in more than
 * one place (for example, once per child process spawned) can esbuild it
 * once and reuse the text, rather than paying a full compile per use.
 */
/**
 * Modules a test wants to fail to load, as a browser does when a lazy chunk
 * cannot be fetched. Each import whose path matches one of `failImports`
 * resolves to a module that throws on evaluation, so a dynamic import() of it
 * rejects with that TypeError.
 */
function failingImports(patterns) {
	return {
		name: 'failing-imports',
		setup(build) {
			for (const filter of patterns) {
				build.onResolve({ filter }, (args) => ({ path: args.path, namespace: 'failing-import' }));
			}
			build.onLoad({ filter: /.*/, namespace: 'failing-import' }, (args) => ({
				contents: `throw new TypeError(${JSON.stringify(
					`Failed to fetch dynamically imported module: ${args.path}`
				)});`,
				loader: 'js'
			}));
		}
	};
}

export async function bundleTypeScriptModule(
	relativePath,
	{ viteApiBase, alias = {}, dev = false, failImports = [] } = {}
) {
	const absolutePath = fileURLToPath(new URL(`../../${relativePath}`, import.meta.url));
	const result = await build({
		entryPoints: [absolutePath],
		absWorkingDir: FRONTEND_ROOT,
		alias: { $lib: LIB_ROOT, ...alias },
		bundle: true,
		define: {
			'$state': 'globalThis.__musicDjToolsTestState',
			'import.meta.env.DEV': dev ? 'true' : 'false',
			'import.meta.env.VITE_API_BASE':
				viteApiBase === undefined ? 'undefined' : JSON.stringify(viteApiBase)
		},
		format: 'esm',
		logLevel: 'silent',
		platform: 'node',
		plugins: [failingImports(failImports), viteUrlSuffixPlugin, svelteComponentStubs],
		target: 'node20',
		write: false
	});
	if (result.outputFiles.length !== 1) {
		throw new Error(`expected one bundled output, got ${result.outputFiles.length}`);
	}
	return result.outputFiles[0].text;
}

/** Bundle one frontend TypeScript module in memory for deterministic unit
 * tests. This avoids Vite's long-lived dependency-optimizer handles. */
export async function loadTypeScriptModule(relativePath, options = {}) {
	const text = await bundleTypeScriptModule(relativePath, options);
	// $state is an identity function under test. $state.snapshot must exist as
	// well or any module whose SETTERS run (rather than just its initializer)
	// dies on `$state.snapshot is not a function` - prefs._persist is the first
	// such case. Runes are compile-time in Svelte; plain structured values are
	// an honest stand-in for the deep-clone the real rune performs.
	globalThis.__musicDjToolsTestState = (value) => value;
	globalThis.__musicDjToolsTestState.snapshot = (value) =>
		value === undefined ? undefined : JSON.parse(JSON.stringify(value));
	// A temp file, not a data: URL: see import-bundled-source.mjs for the
	// 26 MB CI log that the base64 stack frames produced.
	return importBundledSource(text, relativePath);
}
