import { sveltekit } from '@sveltejs/kit/vite';
import { basename } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig, loadEnv } from 'vite';

import { claimAndCheckWebuiDevConfig, resolveAllowedHosts } from './webui-port-config';
import { holdFullReloadPlugin } from './vite-hold-full-reload';

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
		plugins: [sveltekit(), holdFullReloadPlugin()],
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
			// Defaults only, deliberately. `compress.passes: 2` was measured and
			// bought a further 85 bytes, which does not earn a tuning knob that a
			// later reader has to reason about.
			//
			// This does NOT touch the AudioWorklet processors: they are emitted as
			// assets (see assetsInlineLimit below), never as chunks, so the minifier
			// never sees them and the worklet-scope constraints below still hold.
			minify: 'terser' as const,
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
