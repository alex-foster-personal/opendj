import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

import { LIBRARY_JOBS_E2E_FRONTEND_PORT } from './library-jobs-e2e-endpoints';

const frontendPort = Number(process.env.LIBRARY_JOBS_E2E_FRONTEND_PORT ?? LIBRARY_JOBS_E2E_FRONTEND_PORT);
const apiOrigin = process.env.LIBRARY_JOBS_E2E_API_BASE;

if (!Number.isSafeInteger(frontendPort) || apiOrigin === undefined) {
	throw new Error(
		'the library-jobs e2e vite config needs LIBRARY_JOBS_E2E_FRONTEND_PORT and LIBRARY_JOBS_E2E_API_BASE'
	);
}

export default defineConfig({
	plugins: [sveltekit()],
	optimizeDeps: {
		exclude: ['signalsmith-stretch']
	},
	server: {
		host: '127.0.0.1',
		port: frontendPort,
		strictPort: true,
		proxy: {
			'/api': { target: apiOrigin, changeOrigin: false, ws: true }
		}
	}
});
