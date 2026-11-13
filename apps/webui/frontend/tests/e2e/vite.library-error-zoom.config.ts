import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

import { e2eFootprintsPlugin } from '../../vite-e2e-footprints';

import {
	LIBRARY_ERROR_ZOOM_BACKEND_PORT,
	LIBRARY_ERROR_ZOOM_FRONTEND_PORT
} from './library-error-zoom-endpoints';

const frontendPort = Number(
	process.env.LIBRARY_ERROR_ZOOM_FRONTEND_PORT ?? LIBRARY_ERROR_ZOOM_FRONTEND_PORT
);
const backendPort = Number(
	process.env.LIBRARY_ERROR_ZOOM_BACKEND_PORT ?? LIBRARY_ERROR_ZOOM_BACKEND_PORT
);
const apiOrigin = process.env.LIBRARY_ERROR_ZOOM_API_BASE ?? `http://127.0.0.1:${backendPort}`;

if (!Number.isSafeInteger(frontendPort) || !Number.isSafeInteger(backendPort)) {
	throw new Error('library-error-zoom vite config needs valid LIBRARY_ERROR_ZOOM_* ports');
}

export default defineConfig({
	plugins: [sveltekit(), ...e2eFootprintsPlugin()],
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
