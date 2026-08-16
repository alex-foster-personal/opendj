import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let parseWebuiDevConfigPayload;
let resolveWebuiDevConfig;

before(async () => {
	({ parseWebuiDevConfigPayload, resolveWebuiDevConfig } = await loadTypeScriptModule(
		'webui-port-config.ts'
	));
});

test('validated Python JSON payload produces one derived proxy target', () => {
	assert.deepEqual(
		parseWebuiDevConfigPayload(
			JSON.stringify({
				backend: 8697,
				frontend: 9411,
				api_proxy_target: 'http://127.0.0.1:8697'
			})
		),
		{
			backendPort: 8697,
			frontendPort: 9411,
			apiProxyTarget: 'http://127.0.0.1:8697'
		}
	);
	assert.throws(
		() =>
			parseWebuiDevConfigPayload(
				JSON.stringify({
					backend: 8697,
					frontend: 9411,
					api_proxy_target: 'http://localhost:8697'
				})
			),
		/api_proxy_target must equal/
	);
});

test('worktree backend and frontend ports produce one derived proxy target', () => {
	assert.deepEqual(
		resolveWebuiDevConfig({
			MUSIC_DJ_BACKEND_PORT: '8697',
			MUSIC_DJ_FRONTEND_PORT: '9411'
		}),
		{
			backendPort: 8697,
			frontendPort: 9411,
			apiProxyTarget: 'http://127.0.0.1:8697'
		}
	);
});

test('missing, malformed, out-of-range, or colliding ports fail fast', () => {
	assert.throws(
		() => resolveWebuiDevConfig({ MUSIC_DJ_FRONTEND_PORT: '9411' }),
		/MUSIC_DJ_BACKEND_PORT is required/
	);
	assert.throws(
		() =>
			resolveWebuiDevConfig({
				MUSIC_DJ_BACKEND_PORT: 'not-a-port',
				MUSIC_DJ_FRONTEND_PORT: '9411'
			}),
		/MUSIC_DJ_BACKEND_PORT must be an integer/
	);
	assert.throws(
		() =>
			resolveWebuiDevConfig({
				MUSIC_DJ_BACKEND_PORT: '8697',
				MUSIC_DJ_FRONTEND_PORT: '65536'
			}),
		/MUSIC_DJ_FRONTEND_PORT must be between 1024 and 65535/
	);
	assert.throws(
		() =>
			resolveWebuiDevConfig({
				MUSIC_DJ_BACKEND_PORT: '8697',
				MUSIC_DJ_FRONTEND_PORT: '8697'
			}),
		/backend and frontend ports must be different/
	);
});
