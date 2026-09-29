/**
 * Vite's `?url` suffix, modelled for esbuild-based test bundlers.
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
 *
 * Shared by every esbuild-based unit test bundler in this directory
 * (load-typescript.mjs, load-rune-module.mjs, load-svelte-ssr.mjs,
 * mount-svelte.mjs) so a component or module that transitively imports an
 * AudioWorklet processor stays bundleable no matter which harness reaches it.
 */
export const viteUrlSuffixPlugin = {
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
