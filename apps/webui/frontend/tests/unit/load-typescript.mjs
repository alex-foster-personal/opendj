import { existsSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { build } from 'esbuild';

import { importBundledSource } from './import-bundled-source.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const LIB_ROOT = fileURLToPath(new URL('../../src/lib', import.meta.url));

/**
 * Vite's `?url` suffix, modelled for the test bundler.
 *
 * `import x from './y.js?url'` means "give me the URL of y.js as a string" -
 * Vite emits the file as an asset and the import is a plain string. esbuild
 * knows nothing about the suffix and would try to bundle y.js as a module,
 * which fails outright for an AudioWorklet processor (it exports nothing, by
 * construction) and silently inlines the wrong thing for anything else.
 *
 * So: resolve any `?url` import to a stub whose default export is the path.
 * Nothing under test may depend on the VALUE - it is a URL only the browser
 * can act on - which is exactly the contract Vite gives too.
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
export async function bundleTypeScriptModule(relativePath, { viteApiBase, alias = {}, dev = false } = {}) {
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
		plugins: [urlSuffixImports, svelteComponentStubs],
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
