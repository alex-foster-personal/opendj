/**
 * Missing engine source directory bootstrap (issue #10306).
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import {
	parseMissingEngineSourceQuery,
	resetBundledPayload,
	resetBundledPayloadUrl
} from '../../../../desktop/setup/setup.js';

test('missing-engine-source query carries path, key, and health port', () => {
	const search =
		'?missing-engine-source=1&path=%2Ftmp%2Fwt-p0-audio&key=engine_source_directory&health=7701';
	const parsed = parseMissingEngineSourceQuery(search);
	assert.notEqual(parsed, null);
	assert.equal(parsed.path, '/tmp/wt-p0-audio');
	assert.equal(parsed.config_key, 'engine_source_directory');
	assert.equal(parsed.health_port, 7701);
});

test('reset posts to the shell health reset route', async () => {
	let seen;
	const result = await resetBundledPayload({
		healthPort: 7701,
		setStatus: () => {},
		fetchImpl: async (url, init) => {
			seen = { url, init };
		}
	});
	assert.equal(result.ok, true);
	assert.equal(seen.url, resetBundledPayloadUrl(7701));
	assert.equal(seen.init.method, 'POST');
});

test('no health port: reset explains why', async () => {
	const status = [];
	const result = await resetBundledPayload({
		healthPort: null,
		setStatus: (tone, message) => status.push({ tone, message })
	});
	assert.equal(result.ok, false);
	assert.equal(result.reason, 'no-health-port');
	assert.equal(status.at(-1).tone, 'bad');
	assert.match(status.at(-1).message, /cannot reset/i);
});
