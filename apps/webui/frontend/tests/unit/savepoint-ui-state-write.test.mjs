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

test('rejects encoded slash suffixes and encoded traversal on allowed paths', () => {
	// An encoded separator stays in the parsed pathname, so it never equals an allowlist entry.
	assert.equal(isUiStateWrite('PUT', `${ORIGIN}/api/v1/ui-prefs%2F`), false);
	assert.equal(isUiStateWrite('PUT', `${ORIGIN}/api/v1/ui-prefs%2f`), false);
	assert.equal(isUiStateWrite('PUT', `${ORIGIN}/api/v1/ui-prefs%2Fextra`), false);
	assert.equal(isUiStateWrite('PUT', `${ORIGIN}/api/v1/state%2Fui-mirror`), false);
	assert.equal(isUiStateWrite('DELETE', `${ORIGIN}/api/v1/state/ui-mirror%2F`), false);
	assert.equal(isUiStateWrite('PUT', `${ORIGIN}/api/v1/ui-prefs%2F..%2Fplaylists%2Fx`), false);
	assert.equal(isUiStateWrite('PUT', `${ORIGIN}/api/v1/ui-prefs/%2e%2e/playlists/x`), false);
});

test('rejects protocol-relative URLs that only look like an allowed path', () => {
	// A leading '//' makes the first segment the host, so the remaining pathname no longer matches.
	assert.equal(isUiStateWrite('PUT', '//api/v1/ui-prefs'), false);
	assert.equal(isUiStateWrite('PUT', '///api/v1/ui-prefs'), false);
	assert.equal(isUiStateWrite('DELETE', '//api/v1/state/ui-mirror'), false);
	assert.equal(isUiStateWrite('PUT', '//127.0.0.1:9402/api/v1/ui-prefs/extra'), false);
	assert.equal(isUiStateWrite('PUT', '//127.0.0.1:9402/api/v1/ui-prefs'), true);
});

test('ignores query and fragment on exact paths but never lets them smuggle a match', () => {
	// Query and hash are dropped before comparison; they cannot turn a near match into an allowed one.
	assert.equal(isUiStateWrite('PUT', `${ORIGIN}/api/v1/ui-prefs#frag`), true);
	assert.equal(isUiStateWrite('DELETE', `${ORIGIN}/api/v1/state/ui-mirror?a=1#f`), true);
	assert.equal(isUiStateWrite('PUT', `${ORIGIN}/api/v1/ui-prefs/?x=1`), false);
	assert.equal(isUiStateWrite('PUT', `${ORIGIN}/api/v1/state/ui-mirror/lease#x`), false);
	assert.equal(isUiStateWrite('PUT', `${ORIGIN}/api/v1/ui-prefs-evil#/api/v1/ui-prefs`), false);
	assert.equal(isUiStateWrite('PUT', `${ORIGIN}/api/v1/playlists/x?next=/api/v1/ui-prefs`), false);
	assert.equal(isUiStateWrite('PUT', `${ORIGIN}/api/v1/ui-prefs%3Fx=1`), false);
	assert.equal(isUiStateWrite('PUT', `${ORIGIN}/api/v1/ui-prefs%23frag`), false);
});
