import { sveltekit } from '@sveltejs/kit/vite';
import { basename } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig, loadEnv } from 'vite';

import { claimAndCheckWebuiDevConfig, resolveAllowedHosts } from './webui-port-config';

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
	if (devConfig !== null) process.title = `Open DJ · Frontend :${devConfig.frontendPort}`;
	return {
		envDir: REPOSITORY_ROOT,
		plugins: [sveltekit()],
		build: {
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
							'/api': devConfig.apiProxyTarget
						}
					}
				})
	};
});
