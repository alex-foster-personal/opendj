import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const PAGE = readFileSync(
	join(dirname(fileURLToPath(import.meta.url)), '../../src/routes/reconcile/+page.svelte'),
	'utf8'
);

test('reconcile admin view wires remove-from-library without file deletion', () => {
	assert.match(PAGE, /removeFromLibrary\(/);
	assert.match(PAGE, /removeFromLibraryConfirmMessage/);
	assert.match(PAGE, /Remove from library/);
	assert.doesNotMatch(PAGE, /unlink|DELETE|delete file/i);
});
