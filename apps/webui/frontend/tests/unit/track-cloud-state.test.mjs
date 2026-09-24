/**
 * requirement: CHROME-02
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { resolve } from 'node:path';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
let vite;
let cloud;

test.before(async () => {
	vite = await createServer({
		root: FRONTEND_ROOT,
		configFile: false,
		appType: 'custom',
		logLevel: 'silent',
		server: { middlewareMode: true },
		plugins: [svelte()],
		resolve: {
			alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') },
			conditions: ['browser']
		}
	});
	cloud = await vite.ssrLoadModule('/src/lib/components/rb/browser/track-cloud-state.ts');
});

test.after(async () => {
	await vite.close();
});

test('streamingProviderFromPath parses provider URIs', () => {
	assert.equal(cloud.streamingProviderFromPath('spotify:track:abc'), 'spotify');
	assert.equal(cloud.streamingProviderFromPath('tidal:track:1'), 'tidal');
	assert.equal(cloud.streamingProviderFromPath('soundcloud:tracks:9'), 'soundcloud');
	assert.equal(cloud.streamingProviderFromPath('/music/foo.mp3'), 'unknown');
});

test('trackCloudView sets green tick on cloud-and-local', () => {
	const view = cloud.trackCloudView({
		fileExists: true,
		isStreaming: false,
		hasRemoteCopy: true,
		transfer: null
	});
	assert.equal(view.overlay, 'green-tick');
	assert.equal(view.provider, null);
});

test('trackCloudView sets blue tick on local-only', () => {
	const view = cloud.trackCloudView({
		fileExists: true,
		isStreaming: false,
		hasRemoteCopy: false,
		transfer: null
	});
	assert.equal(view.overlay, 'blue-tick');
});

test('streaming rows expose provider without tick overlay', () => {
	const view = cloud.trackCloudView({
		fileExists: false,
		isStreaming: true,
		hasRemoteCopy: false,
		folderPath: 'spotify:track:x',
		transfer: null
	});
	assert.equal(view.kind, 'streaming');
	assert.equal(view.provider, 'spotify');
	assert.equal(view.overlay, 'none');
});

test('CloudStatusIcon component exists for TrackTable', () => {
	const table = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url)),
		'utf8'
	);
	const icon = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/browser/CloudStatusIcon.svelte', import.meta.url)),
		'utf8'
	);
	assert.match(table, /<CloudStatusIcon\b/);
	assert.match(icon, /tick-green|tick-blue|provider-icon/);
});
