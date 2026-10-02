import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

import { VOCALS_DEMUCS_OVERLAY_FRONTEND_PORT } from './vocals-demucs-overlay-endpoints';

const frontendPort = Number(
	process.env.VOCALS_DEMUCS_OVERLAY_FRONTEND_PORT ?? VOCALS_DEMUCS_OVERLAY_FRONTEND_PORT
);
const apiOrigin = process.env.VOCALS_DEMUCS_OVERLAY_API_BASE;

if (!Number.isSafeInteger(frontendPort) || apiOrigin === undefined) {
	throw new Error(
		'vocals demucs overlay vite config needs VOCALS_DEMUCS_OVERLAY_FRONTEND_PORT and VOCALS_DEMUCS_OVERLAY_API_BASE'
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
