/**
 * Vite dev server for the Cmd K time-to-execute harness (CMDK-9468).
 */
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export const CMDK_TTX_FRONTEND_PORT = 5338;
export const CMDK_TTX_API_PORT = 8718;

export default defineConfig({
	plugins: [sveltekit()],
	optimizeDeps: {
		// The stretch worklet must not be pre-bundled, or it never posts ready.
		exclude: ['signalsmith-stretch']
	},
	server: {
		host: '127.0.0.1',
		port: CMDK_TTX_FRONTEND_PORT,
		strictPort: true,
		proxy: {
			'/api': {
				target: `http://127.0.0.1:${CMDK_TTX_API_PORT}`,
				changeOrigin: false
			}
		}
	}
});
