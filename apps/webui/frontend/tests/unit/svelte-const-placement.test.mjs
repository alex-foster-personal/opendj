// [if] a `{@const}` sits as a raw child of `<td>` [then] vite-plugin-svelte throws const_tag_invalid_placement and /performance hydrates blank
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const FRONTEND = fileURLToPath(new URL('../..', import.meta.url));
const SRC = join(FRONTEND, 'src');
const RAW_TD_CONST = /<td\b[^>]*>\s*\{@const\b/;

function svelteFiles(dir, acc = []) {
	for (const entry of readdirSync(dir, { withFileTypes: true })) {
		const path = join(dir, entry.name);
		if (entry.isDirectory()) svelteFiles(path, acc);
		else if (entry.name.endsWith('.svelte')) acc.push(path);
	}
	return acc;
}

test('no Svelte file places {@const} directly under <td>', () => {
	const hits = [];
	for (const file of svelteFiles(SRC)) {
		const text = readFileSync(file, 'utf8');
		if (RAW_TD_CONST.test(text)) hits.push(file.slice(FRONTEND.length + 1));
	}
	assert.deepEqual(hits, [], `const_tag_invalid_placement in: ${hits.join(', ')}`);
});

test('TrackTable cloud cell wraps {@const} in {#if row}', () => {
	const text = readFileSync(join(SRC, 'lib/components/rb/browser/TrackTable.svelte'), 'utf8');
	assert.match(text, /<td class="c-cloud">\s*\{#if row\}\s*\{@const cloudView/);
	assert.doesNotMatch(text, /<td class="c-cloud">\s*\{@const cloudView/);
});
