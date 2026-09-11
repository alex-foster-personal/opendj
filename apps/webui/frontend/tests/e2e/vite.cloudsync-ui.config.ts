/**
 * Vite dev server for the /cloudsync UI e2e (playwright.cloudsync-ui.config.ts).
 * Proxies /api to the SPOKE engine. changeOrigin stays false so the engine
 * sees the loopback Host the browser used; the CloudSync operator routes
 * refuse any non-loopback Host (apps/webui/server/local_operator.py).
 */
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

const frontendPort = Number(process.env.CLOUDSYNC_UI_FRONTEND_PORT);
const apiPort = Number(process.env.CLOUDSYNC_UI_API_PORT);
if (!Number.isInteger(frontendPort) || !Number.isInteger(apiPort)) {
	throw new Error(
		'vite.cloudsync-ui.config.ts needs CLOUDSYNC_UI_FRONTEND_PORT and ' +
			'CLOUDSYNC_UI_API_PORT set (playwright.cloudsync-ui.config.ts sets both).'
	);
}

export default defineConfig({
	plugins: [sveltekit()],
	server: {
		host: '127.0.0.1',
		port: frontendPort,
		strictPort: true,
		proxy: {
			'/api': {
				target: `http://127.0.0.1:${apiPort}`,
				changeOrigin: false
			}
		}
	}
});
