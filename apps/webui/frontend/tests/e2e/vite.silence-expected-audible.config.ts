/**
 * Vite dev server for silence-expected-audible e2e (#6636). Proxies /api to the
 * real backend booted by playwright.silence-expected-audible.config.ts.
 */
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export const SILENCE_EXPECTED_AUDIBLE_FRONTEND_PORT = 5327;
export const SILENCE_EXPECTED_AUDIBLE_API_PORT = 8707;

export default defineConfig({
	plugins: [sveltekit()],
	server: {
		host: '127.0.0.1',
		port: SILENCE_EXPECTED_AUDIBLE_FRONTEND_PORT,
		strictPort: true,
		proxy: {
			'/api': {
				target: `http://127.0.0.1:${SILENCE_EXPECTED_AUDIBLE_API_PORT}`,
				changeOrigin: false
			}
		}
	}
});
