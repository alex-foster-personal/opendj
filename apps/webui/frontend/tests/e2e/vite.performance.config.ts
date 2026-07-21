import { sveltekit } from '@sveltejs/kit/vite';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import type { Plugin } from 'vite';

const DEFAULT_FRONTEND_BASE = 'http://127.0.0.1:5273';
const DEFAULT_API_BASE = 'http://127.0.0.1:8686';
const TONE_FIXTURE_PATH = fileURLToPath(
	new URL('../../../../../tests/fixtures/phase7-dedup/src.wav', import.meta.url)
);
const TONE_FIXTURE = readFileSync(TONE_FIXTURE_PATH);
const TONE_FIXTURE_URL = '/__performance_e2e__/tone.wav';

if (TONE_FIXTURE.byteLength <= 44) {
	throw new Error(`PCM tone fixture is missing audio data: ${TONE_FIXTURE_PATH}`);
}

function _toneFixturePlugin(): Plugin {
	return {
		name: 'performance-e2e-tone-fixture',
		configureServer(server) {
			server.middlewares.use((request, response, next) => {
				const pathname = new URL(request.url ?? '/', 'http://performance-e2e.local').pathname;
				if (pathname !== TONE_FIXTURE_URL) {
					next();
					return;
				}
				if (request.method !== 'GET' && request.method !== 'HEAD') {
					response.statusCode = 405;
					response.setHeader('Allow', 'GET, HEAD');
					response.end('method not allowed');
					return;
				}
				response.statusCode = 200;
				response.setHeader('Cache-Control', 'no-store');
				response.setHeader('Content-Length', String(TONE_FIXTURE.byteLength));
				response.setHeader('Content-Type', 'audio/wav');
				response.end(request.method === 'HEAD' ? undefined : TONE_FIXTURE);
			});
		}
	};
}

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
	plugins: [_toneFixturePlugin(), sveltekit()],
	optimizeDeps: {
		// The package serializes its own module function into an AudioWorklet.
		// Esbuild pre-bundling rewrites that function and the processor never
		// posts its ready message, so this exclusion is a runtime requirement.
		exclude: ['signalsmith-stretch']
	},
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
