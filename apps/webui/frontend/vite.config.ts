import { sveltekit } from '@sveltejs/kit/vite';
import { basename } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';

import { claimAndCheckWebuiDevConfig, parseAllowedHosts } from './webui-port-config';

const REPOSITORY_ROOT = fileURLToPath(new URL('../../../', import.meta.url));

export default defineConfig(({ command }) => {
	const isDevServer =
		command === 'serve' &&
		(process.env.npm_lifecycle_event === 'dev' ||
			basename(process.argv[1] ?? '') === 'vite.js');
	const devConfig = isDevServer
		? claimAndCheckWebuiDevConfig(REPOSITORY_ROOT, 'frontend')
		: null;
	return {
		envDir: REPOSITORY_ROOT,
		plugins: [sveltekit()],
		optimizeDeps: {
			// Prebundling rewrites Signalsmith's self-stringifying AudioWorklet
			// module and causes processor creation to time out.
			exclude: ['signalsmith-stretch']
		},
		server:
			devConfig === null
				? undefined
				: {
						port: devConfig.frontendPort,
						strictPort: true,
						allowedHosts: parseAllowedHosts(process.env.MUSIC_DJ_ALLOWED_HOSTS),
						proxy: {
							'/api': devConfig.apiProxyTarget
						}
					}
	};
});
