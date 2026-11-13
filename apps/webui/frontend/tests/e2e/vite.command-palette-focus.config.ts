/**
 * Vite dev server for the Cmd K focus-return gate (issue #12947).
 */
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export const CMDK_FOCUS_FRONTEND_PORT = 5339;
export const CMDK_FOCUS_API_PORT = 8719;

export default defineConfig({
	plugins: [sveltekit()],
	optimizeDeps: {
		exclude: ['signalsmith-stretch']
	},
	server: {
		host: '127.0.0.1',
		port: CMDK_FOCUS_FRONTEND_PORT,
		strictPort: true,
		proxy: {
			'/api': {
				target: `http://127.0.0.1:${CMDK_FOCUS_API_PORT}`,
				changeOrigin: false
			}
		}
	}
});
