import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://mytags-api.example.test';
const REVISION = '"catalog-revision"';

let api;
let originalFetch;

before(async () => {
	api = await loadTypeScriptModule('src/lib/rb/api-edit-suite.ts', { viteApiBase: API_BASE });
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('MyTag client reads the catalog revision and sends destructive scope acknowledgements', async () => {
	const requests = [];
	globalThis.fetch = async (input, init) => {
		requests.push({ url: String(input), init });
		if (String(input).endsWith('/mytags')) {
			return Response.json({
				tags: [{ name: 'warmup', track_count: 3 }],
				catalog_revision: REVISION
			});
		}
		return Response.json({ tracks_updated: 3 });
	};

	assert.deepEqual(await api.listMyTags(), {
		tags: [{ name: 'warmup', track_count: 3 }],
		catalog_revision: REVISION
	});
	await api.renameMyTag({
		old_name: 'warmup',
		new_name: 'opening',
		expected_catalog_revision: REVISION,
		expected_track_count: 3,
		confirm_merge: true
	});
	await api.deleteMyTag({
		name: 'opening',
		expected_catalog_revision: REVISION,
		expected_track_count: 3
	});

	assert.equal(requests[1].url, `${API_BASE}/api/v1/mytags/rename`);
	assert.deepEqual(JSON.parse(requests[1].init.body), {
		old_name: 'warmup',
		new_name: 'opening',
		expected_catalog_revision: REVISION,
		expected_track_count: 3,
		confirm_merge: true
	});
	assert.equal(requests[2].url, `${API_BASE}/api/v1/mytags/delete`);
	assert.deepEqual(JSON.parse(requests[2].init.body), {
		name: 'opening',
		expected_catalog_revision: REVISION,
		expected_track_count: 3
	});
});
