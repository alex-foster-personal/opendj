import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const HERE = dirname(fileURLToPath(import.meta.url));
const PAGE = readFileSync(join(HERE, '../../src/routes/reconcile/+page.svelte'), 'utf8');
const CLIENT = readFileSync(join(HERE, '../../src/lib/rb/track-library.ts'), 'utf8');

// LIBM-78b (pin 07270393c8d1): the /reconcile missing-files view offers
// remove-from-library per row and never deletes files. The page calls the shared
// track-library client; tests/webui/test_track_remove_undelete.py proves that
// endpoint leaves the audio file untouched.
test('LIBM-78b: reconcile rows remove through the shared :remove client, never a file delete', () => {
	assert.match(PAGE, /import \{ removeFromLibrary \} from '\$lib\/rb\/track-library'/);
	assert.match(PAGE, /onclick=\{\(\) => void removeFromLibraryRow\(track\)\}/);
	assert.match(CLIENT, /api\.POST\('\/api\/v1\/tracks\/\{stable_id\}:remove'/);
	assert.doesNotMatch(CLIENT, /DELETE|unlink/);
});

test('reconcile admin view wires remove-from-library without file deletion', () => {
	assert.match(PAGE, /removeFromLibrary\(/);
	assert.match(PAGE, /removeFromLibraryConfirmMessage/);
	assert.match(PAGE, /Remove from library/);
	assert.doesNotMatch(PAGE, /unlink|DELETE|delete file/i);
});
