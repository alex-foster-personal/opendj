import { sveltekit } from '@sveltejs/kit/vite';
import { basename } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig, loadEnv } from 'vite';

import { claimAndCheckWebuiDevConfig, resolveAllowedHosts } from './webui-port-config';
import { codecParserTrimPlugin } from './vite-codec-parser-trim';
import { holdFullReloadPlugin } from './vite-hold-full-reload';
import { bootManualChunks } from './vite-layout-shell-chunk';

const REPOSITORY_ROOT = fileURLToPath(new URL('../../../', import.meta.url));

export default defineConfig(({ command, mode }) => {
	// vite does not put the root .env on process.env at config time, so a value
	// only present there is invisible unless it is loaded explicitly.
	const rootEnv = loadEnv(mode, REPOSITORY_ROOT, 'MUSIC_DJ_');
	const isDevServer =
		command === 'serve' &&
		(process.env.npm_lifecycle_event === 'dev' ||
			basename(process.argv[1] ?? '') === 'vite.js');
	const devConfig = isDevServer
		? claimAndCheckWebuiDevConfig(REPOSITORY_ROOT, 'frontend')
		: null;
	if (devConfig !== null) {
		process.title = `opendj-frontend --name opendj-frontend --port ${devConfig.frontendPort}`;
	}
	// The one switch for what opening the audio I/O view may do (IOPIN-14). The
	// dev server keeps a fixed origin, so the browser remembers a device-access
	// grant and asking on open costs one prompt, once. Every built app (the
	// packaged shell, a production bundle) must not prompt unasked and shows a
	// grant button instead. Read by `ioDeviceAccessOnOpen`.
	const ioDeviceAccessOnOpen: 'request' | 'button' = devConfig === null ? 'button' : 'request';
	return {
		envDir: REPOSITORY_ROOT,
		define: {
			'import.meta.env.VITE_IO_DEVICE_ACCESS_ON_OPEN': JSON.stringify(ioDeviceAccessOnOpen)
		},
		plugins: [sveltekit(), holdFullReloadPlugin(), codecParserTrimPlugin()],
		build: {
			// Terser instead of Vite's default esbuild minifier. Measured on
			// origin/main at 59248abb9, gzip under build/_app/immutable/, which is
			// exactly what scripts/bundle-budget.mjs weighs:
			//   library      105,664 -> 101,056   (-4,608)
			//   performance  205,141 -> 193,290  (-11,851, 94.9% -> 89.5%)
			//   other-lazy    65,348 ->  62,014   (-3,334)
			// About 6% off every surface, 19,793 bytes in total, for 0.4s of build
			// time (13.0s -> 13.4s). Vite runs terser in worker threads, so it is
			// near free here. The 205,141 baseline reproduces the `measured` value
			// already recorded for the performance budget, so these deltas are on
			// the same footing as the numbers the ceilings were derived from.
			//
			// The combined main + Preview graph needs another compression pass
			// over function declarations. Terser hoists declarations that JavaScript
			// already hoists; extra safe compression passes then remove their
			// repeated wrappers. No unsafe transforms or property mangling.
			// Sat 3 Oct 2026, same source graph as 028d06401 + explicit UI chunks:
			// library 255762 -> 257475, performance 249829 -> 247002,
			// other-lazy 298278 -> 296855 gzip bytes. The total falls by 2537.
			// Worklet assets bypass minification. Real UI/runtime checks still apply.
			//
			// This does NOT touch the AudioWorklet processors: they are emitted as
			// assets (see assetsInlineLimit below), never as chunks, so the minifier
			// never sees them and the worklet-scope constraints below still hold.
			// Vite's default target is es2020 with chrome87, edge88, firefox78 and
			// safari14. esbuild rewrites every optional chain (`a?.b`) into a
			// `null == (t = a) ? void 0 : t.b` ternary for Chrome and Edge below 91,
			// because of a V8 defect in those versions. Nothing this app ships on is
			// that old: the packaged shell is a WKWebView on macOS 11 or later
			// (Safari 14, which is kept here and has native optional chaining), and
			// a browser session is a current Chrome. Only the two Chromium floors
			// move, to the first version esbuild trusts with the syntax.
			// Measured Thu 1 Oct 2026 on af--preview-mixtour-io d07590614e, gzip as
			// scripts/bundle-budget.mjs weighs it: library 251,739 -> 249,183,
			// performance 241,532 -> 238,992, other-lazy 285,882 -> 284,263.
			target: ['es2020', 'chrome91', 'edge91', 'firefox78', 'safari14'],
			minify: 'terser' as const,
			// One exception to "defaults only". Vite forces terser's `safari10`
			// workarounds on (loop-scoped `let` and `await` naming bugs in Safari
			// 10 and 11). The build target is Vite's default, which starts at
			// Safari 14, and the packaged shell is a current WKWebView, so the
			// workaround guards nothing here. Measured Thu 1 Oct 2026 on
			// af--preview-mixtour-io 7e14328ea0: library 270,039 -> 269,310,
			// performance 242,107 -> 241,529, other-lazy 293,004 -> 292,698.
			terserOptions: { safari10: false, ecma: 2020, compress: { passes: 5, hoist_funs: true } },
			rollupOptions: {
				// See vite-layout-shell-chunk.ts for what this merges, the
				// measured bytes, and why it does not change behavior.
				output: { manualChunks: bootManualChunks(), onlyExplicitManualChunks: true }
			},
			// AudioWorklet modules must stay REAL FILES. Anything under the
			// default 4096-byte inline limit is emitted as a `data:` URI, and
			// `audioWorklet.addModule()` fetches a module script: a data: URI
			// gives it an opaque origin, which browsers refuse. The xrun sentinel
			// is ~3.5KB, i.e. exactly in the range where it would silently become
			// a URI that fails to load in the packaged app and nowhere else.
			assetsInlineLimit: (filePath: string) =>
				filePath.endsWith('-processor.js') ? false : undefined
		},
		optimizeDeps: {
			// Prebundling rewrites Signalsmith's self-stringifying AudioWorklet
			// module and causes processor creation to time out.
			exclude: ['signalsmith-stretch']
		},
		...(devConfig === null
			? {}
			: {
					server: {
						port: devConfig.frontendPort,
						strictPort: true,
						allowedHosts: resolveAllowedHosts(process.env, rootEnv),
						proxy: {
							// ws: true is load-bearing. Playlist undo/redo and every
							// other library.changed consumer sit on /api/v1/events;
							// without the upgrade the tree never hears a rename
							// (#1888) and the history panel never enables undo.
							'/api': {
								target: devConfig.apiProxyTarget,
								changeOrigin: true,
								ws: true
							},
							'/sets/shared': devConfig.apiProxyTarget
						}
					}
				})
	};
});
