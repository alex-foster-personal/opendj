import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const { isUiStateWrite } = await loadTypeScriptModule('tests/e2e/savepoint-endpoints.ts');

const ORIGIN = 'http://127.0.0.1:9402';

test('allowlists PUT ui-prefs and ui-mirror and DELETE ui-mirror', () => {
	assert.equal(isUiStateWrite('PUT', `${ORIGIN}/api/v1/ui-prefs`), true);
	assert.equal(isUiStateWrite('PUT', `${ORIGIN}/api/v1/state/ui-mirror`), true);
	assert.equal(isUiStateWrite('DELETE', `${ORIGIN}/api/v1/state/ui-mirror`), true);
	assert.equal(isUiStateWrite('PUT', `${ORIGIN}/api/v1/state/ui-mirror?x=1`), true);
	assert.equal(isUiStateWrite('PUT', '/api/v1/ui-prefs'), true);
});

test('rejects catalog mutations and wrong verbs', () => {
	assert.equal(isUiStateWrite('PUT', `${ORIGIN}/api/v1/playlists/x`), false);
	assert.equal(isUiStateWrite('DELETE', `${ORIGIN}/api/v1/ui-prefs`), false);
	assert.equal(isUiStateWrite('PUT', `${ORIGIN}/api/v1/ui-prefs/../playlists/x`), false);
});

test('rejects prefix, suffix, traversal, and malformed URLs', () => {
	assert.equal(isUiStateWrite('PUT', `${ORIGIN}/api/v1/ui-prefs/`), false);
	assert.equal(isUiStateWrite('PUT', `${ORIGIN}/api/v1/ui-prefs/extra`), false);
	assert.equal(isUiStateWrite('PUT', `${ORIGIN}/api/v1/ui-prefs-evil`), false);
	assert.equal(isUiStateWrite('PUT', `${ORIGIN}/api/v1/state/ui-mirror/lease`), false);
	assert.equal(isUiStateWrite('PATCH', `${ORIGIN}/api/v1/ui-prefs`), false);
	assert.equal(isUiStateWrite('PUT', 'not-a-url'), false);
});
