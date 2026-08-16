import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let parseAllowedHosts;
let parseWebuiDevConfigPayload;
let resolveWebuiDevConfig;

before(async () => {
	({ parseAllowedHosts, parseWebuiDevConfigPayload, resolveWebuiDevConfig } =
		await loadTypeScriptModule('webui-port-config.ts'));
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

test('unset MUSIC_DJ_ALLOWED_HOSTS keeps vite loopback-only', () => {
	assert.deepEqual(parseAllowedHosts(undefined), []);
	assert.deepEqual(parseAllowedHosts(''), []);
	assert.deepEqual(parseAllowedHosts('   '), []);
});

test('a remote runner hostname reaches vite through its proxy', () => {
	assert.deepEqual(parseAllowedHosts('agentbox.example-tailnet.ts.net'), [
		'agentbox.example-tailnet.ts.net'
	]);
	assert.deepEqual(parseAllowedHosts(' agentbox , agentbox.example-tailnet.ts.net '), [
		'agentbox',
		'agentbox.example-tailnet.ts.net'
	]);
});

test('wildcards, ports and schemes are rejected rather than silently widening the allowlist', () => {
	for (const rejected of [
		'*',
		'.ts.net',
		'*.ts.net',
		'agentbox:8080',
		'http://agentbox',
		'agentbox/performance'
	]) {
		assert.throws(
			() => parseAllowedHosts(rejected),
			/MUSIC_DJ_ALLOWED_HOSTS must list bare hostnames/,
			`expected ${rejected} to be rejected`
		);
	}
	assert.throws(
		() => parseAllowedHosts(', ,'),
		/MUSIC_DJ_ALLOWED_HOSTS was set but named no hostname/
	);
	// 'true' is a legal hostname label, so it is kept - as the literal string.
	// What must never happen is it becoming vite's allow-every-host boolean.
	assert.deepEqual(parseAllowedHosts('true'), ['true']);
	assert.notEqual(parseAllowedHosts('true'), true);
});
