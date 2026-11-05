/**
 * Copy and hover nits from the public-launch UI sweep (Wed 7 Oct 2026,
 * orders board #5638 N7, N9, N13). Each guard reads the shipped source.
 */
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const SRC = fileURLToPath(new URL('../../src/', import.meta.url));

function source(relative) {
	return readFileSync(join(SRC, relative), 'utf8');
}

function svelteFiles(dir) {
	const out = [];
	for (const name of readdirSync(dir)) {
		const path = join(dir, name);
		if (statSync(path).isDirectory()) out.push(...svelteFiles(path));
		else if (name.endsWith('.svelte')) out.push(path);
	}
	return out;
}

/** A requirement id such as LIBUX-06 or UX-FLOAT-03. */
const REQ_ID = /^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d+$/;

test('no page eyebrow shows a requirement id (the Library wheel said "LIBUX-06")', () => {
	const files = svelteFiles(join(SRC, 'routes'));
	assert.ok(files.length > 20, `expected the routes tree, found ${files.length} files`);
	const eyebrows = [];
	for (const file of files) {
		for (const m of readFileSync(file, 'utf8').matchAll(/class="eyebrow"[^>]*>([^<{]+)</g)) {
			eyebrows.push({ file, text: m[1].trim() });
		}
	}
	assert.ok(eyebrows.length >= 3, `expected the static eyebrows (SETS, SESSION HISTORY, ...), found ${eyebrows.length}`);
	const leaked = eyebrows.filter((e) => REQ_ID.test(e.text));
	assert.deepEqual(leaked, []);
});

test('control: the requirement-id pattern catches the original eyebrow and spares real words', () => {
	assert.ok(REQ_ID.test('LIBUX-06'));
	assert.ok(REQ_ID.test('UX-FLOAT-03'));
	assert.ok(!REQ_ID.test('LIBRARY'));
	assert.ok(!REQ_ID.test('SESSION HISTORY'));
});

test('the deck header title and artist carry a hover tip, so an ellipsized name can be read', () => {
	const header = source('lib/components/rb/deck/DeckHeader.svelte');
	// LIBUX-57: the title's tip shows only when it is cut off (truncation-tip.ts).
	assert.match(header, /<span class="title" data-custom-tip="" use:truncationTip>/);
	assert.match(header, /<span class="artist" title=\{deck\.artist \|\| undefined\}>/);
});

test('the Pairings form does not ask users for a "stable_id"', () => {
	const page = source('routes/pairings/+page.svelte');
	assert.doesNotMatch(page, /placeholder="[^"]*stable_id/);
	assert.match(page, /placeholder="From track ID"/);
	assert.match(page, /placeholder="To track ID"/);
});
