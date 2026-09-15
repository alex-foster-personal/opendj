/**
 * Boot-mode plain language for PREFLIGHT-01 (issue #2722 P1-1).
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let bootCopy;

before(async () => {
	bootCopy = await loadTypeScriptModule('src/lib/preflight/preflight-boot-copy.ts');
});

function check(overrides = {}) {
	return {
		id: 'engine-alive',
		label: 'Engine alive',
		status: 'pass',
		detail: 'the endpoint answered',
		remediation: null,
		...overrides
	};
}

test('boot mode maps technical labels to plain language', () => {
	assert.equal(bootCopy.bootCheckLabel(check(), 'boot'), 'Starting up');
	assert.equal(
		bootCopy.bootCheckLabel(check({ id: 'state-db', label: 'State database' }), 'boot'),
		'Your library'
	);
	assert.equal(
		bootCopy.bootCheckLabel(check({ id: 'library-attached', label: 'Library attached' }), 'boot'),
		'Music library'
	);
});

test('admin mode keeps server labels verbatim', () => {
	const row = check({ id: 'state-db', label: 'State database' });
	assert.equal(bootCopy.bootCheckLabel(row, 'admin'), 'State database');
});

test('boot mode hides schema_meta jargon in state-db details', () => {
	const technical = check({
		id: 'state-db',
		label: 'State database',
		status: 'fail',
		detail: 'schema_meta version 14 (expected 14)'
	});
	const plain = bootCopy.bootCheckDetail(technical, 'boot');
	assert.doesNotMatch(plain, /schema_meta/i);
	assert.match(plain, /update/i);
});

test('scrubBootDetail removes schema_meta version strings', () => {
	assert.doesNotMatch(
		bootCopy.scrubBootDetail('schema_meta reports version 3, expected 14'),
		/schema_meta/i
	);
});
