// Sol P2 on PR #4014: a second browser confirm (delete or drop) opened while
// one is pending must settle the first, as a cancel, instead of overwriting
// its resolver and leaving that operation awaiting forever. The cancel must
// not read as ok:false alone, because for a drop ok:false means Move.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = readFileSync(join(root, 'src/lib/components/rb/BrowserPanel.svelte'), 'utf8');

function askBody() {
	const start = src.indexOf('function askBrowserConfirm(');
	assert.ok(start >= 0, 'askBrowserConfirm must exist');
	return src.slice(start, src.indexOf('\n\t}\n', start));
}

test('askBrowserConfirm settles the pending confirm as cancelled before replacing it', () => {
	const body = askBody();
	const settle = body.search(
		/browserConfirmPending\?\.resolve\(\{ ok: false, remember: false, setDefault: false, cancelled: true \}\);/
	);
	const replace = body.indexOf('browserConfirmPending = {');
	assert.ok(settle >= 0, 'the superseded confirm must be resolved with cancelled: true');
	assert.ok(replace > settle, 'it must be settled BEFORE the new request replaces it');
});

test('a cancelled drop confirm aborts instead of falling through to Move', () => {
	assert.match(
		src,
		/if \(choice\.cancelled\) return;\s*mode = choice\.ok \? 'add' : 'move';/,
		'dropTracksOnPlaylist must return on a cancelled confirm before mapping ok:false to move'
	);
	// Control: delete already aborts on any non-ok answer, cancel included.
	assert.match(src, /title: 'Delete playlist',[\s\S]*?if \(!choice\.ok\) return;/);
});
