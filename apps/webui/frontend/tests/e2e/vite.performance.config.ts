import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

const DEFAULT_FRONTEND_BASE = 'http://127.0.0.1:5273';
const DEFAULT_API_BASE = 'http://127.0.0.1:8686';

function _loopbackUrl(name: string, raw: string): URL {
	const url = new URL(raw);
	if (url.protocol !== 'http:') {
		throw new Error(`${name} must use http, got ${url.protocol}`);
	}
	if (url.hostname !== '127.0.0.1' && url.hostname !== 'localhost') {
		throw new Error(`${name} must be loopback, got ${url.hostname}`);
	}
	if (url.port === '') {
		throw new Error(`${name} must include an explicit port`);
	}
	if (url.pathname !== '/' || url.search !== '' || url.hash !== '') {
		throw new Error(`${name} must be an origin without path, query, or fragment`);
	}
	return url;
}

const frontend = _loopbackUrl(
	'PERFORMANCE_E2E_BASE_URL',
	process.env.PERFORMANCE_E2E_BASE_URL ?? DEFAULT_FRONTEND_BASE
);
const api = _loopbackUrl(
	'PERFORMANCE_E2E_API_BASE',
	process.env.PERFORMANCE_E2E_API_BASE ?? DEFAULT_API_BASE
);

export default defineConfig({
	plugins: [sveltekit()],
	server: {
		host: frontend.hostname,
		port: Number(frontend.port),
		strictPort: true,
		proxy: {
			'/api': {
				target: api.origin,
				changeOrigin: false
			}
		}
	}
});
