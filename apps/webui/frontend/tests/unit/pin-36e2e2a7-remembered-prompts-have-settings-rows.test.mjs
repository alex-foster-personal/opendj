/**
 * pin 36e2e2a7 (remainder): "anything where user might want to set something
 * every time should have this as well as config in settings for changing it /
 * resetting each of the 'dont ask again' states".
 *
 * Every prompt that can remember its answer stores it under `uiPrefs.confirm`
 * (prefs.svelte.ts). LIBUX-32 gave the playlist-drop answer its row; this pins
 * the INVARIANT rather than the list: whatever keys `confirm` carries, each
 * one has a Settings > Confirmations row that can read it and reset it. A new
 * remembered prompt that ships without a row fails here.
 *
 * - if a remembered answer has no settings row then it is permanent -> broken
 * - if a row exists but the apply layer cannot read or write it then the row
 *   is inert -> broken
 * - if a prompt remembers under a key the prefs type does not declare then
 *   this invariant cannot see it -> broken
 * - if the delete row hides that it also governs smartlist deletes then the
 *   operator cannot find the switch for that prompt -> broken
 */
import assert from 'node:assert/strict';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const srcDir = fileURLToPath(new URL('../../src', import.meta.url));
const read = (rel) => readFileSync(path.join(srcDir, rel), 'utf8');

/** The keys of `confirm: { ... }` in the RbUiPrefs interface. */
function declaredConfirmKeys() {
	const prefs = read('lib/rb/prefs.svelte.ts');
	const block = /\n\tconfirm: \{\n([\s\S]*?)\n\t\};/.exec(prefs);
	assert.ok(block, 'expected the RbUiPrefs confirm block');
	const keys = [...block[1].matchAll(/^\t\t([a-z_]+)\?:/gm)].map((m) => m[1]);
	assert.ok(keys.length >= 3, `control: the parser must find the known keys, found ${keys}`);
	assert.ok(keys.includes('playlist_drop_mode'), 'control: a key known to exist must be found');
	return keys;
}

function sourceFiles(dir) {
	const out = [];
	for (const name of readdirSync(dir)) {
		const full = path.join(dir, name);
		if (statSync(full).isDirectory()) out.push(...sourceFiles(full));
		else if (/\.(svelte|ts)$/.test(name)) out.push(full);
	}
	return out;
}

let confirmSettings;
before(async () => {
	confirmSettings = (await loadTypeScriptModule('src/lib/settings/confirm-drop-mode.ts')).CONFIRM_SETTINGS;
});

test('every remembered prompt answer has an implemented Confirmations row', () => {
	const rows = new Map(confirmSettings.map((row) => [row.id, row]));
	for (const key of declaredConfirmKeys()) {
		const row = rows.get(`confirm.${key}`);
		assert.ok(row, `confirm.${key} is remembered but has no settings row, so it can never be reset`);
		assert.equal(row.group, 'confirmations');
		assert.equal(row.implemented, true, `confirm.${key} must be a live control, not a planned one`);
	}
	assert.equal(
		rows.size,
		declaredConfirmKeys().length,
		'a Confirmations row with no remembered answer behind it is a control wired to nothing'
	);
});

test('the apply layer can read and write every remembered answer', () => {
	const apply = read('lib/settings/apply.ts');
	for (const key of declaredConfirmKeys()) {
		const id = `confirm.${key}`;
		const cases = apply.match(new RegExp(`case '${id.replace('.', '\\.')}':`, 'g')) ?? [];
		assert.equal(cases.length, 2, `${id} needs one read case and one write case in apply.ts`);
		assert.match(apply, new RegExp(`\\n\\t'${id.replace('.', '\\.')}',`), `${id} must be in the apply allowlist`);
		assert.match(
			apply,
			new RegExp(`setConfirmPref\\('${key}',`),
			`${id} must be written through setConfirmPref so a reset reaches disk`
		);
	}
});

test('no prompt remembers its answer under an undeclared key', () => {
	const declared = new Set(declaredConfirmKeys());
	const used = new Set();
	for (const file of sourceFiles(srcDir)) {
		for (const match of readFileSync(file, 'utf8').matchAll(/setConfirmPref\(\s*'([a-z_]+)'/g)) {
			used.add(match[1]);
		}
	}
	assert.ok(used.size >= 3, `control: the scan must find the known call sites, found ${[...used]}`);
	for (const key of used) {
		assert.ok(declared.has(key), `setConfirmPref('${key}') remembers under a key the prefs type does not declare`);
	}
});

test('the delete row says it also governs smartlist deletes', () => {
	const tree = read('lib/components/rb/browser/TreeSmartlistSection.svelte');
	assert.match(
		tree,
		/uiPrefs\.confirm\.delete_playlist === false/,
		'control: the smartlist delete prompt really is governed by this answer'
	);
	const row = confirmSettings.find((r) => r.id === 'confirm.delete_playlist');
	assert.match(row.label, /smartlist/i);
	assert.match(row.title, /smartlist/i);
	assert.ok(row.keywords.includes('smartlist'), 'searching settings for smartlist must find the row');
	assert.match(row.detail, /turn it back on to be asked again/i);
});

test('each boolean confirmation row says how to get the prompt back', () => {
	for (const row of confirmSettings.filter((r) => r.control.kind === 'boolean')) {
		assert.match(row.detail, /turn it back on to be asked again/i, `${row.id} must say how to reset it`);
	}
});
