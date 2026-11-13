// #12287: `opendj hotkeys` reads the overlay rows from
// apps/opendj_cli/hotkeys_contract.json, written by
// scripts/export-hotkeys-contract.mjs. This keeps that export in step with the
// TypeScript registry and pins the per-platform and flag fields it carries.
//
// Regression lines (single-line format per CLAUDE.md):
//   if an overlay row changes without rerunning the export then broken
//   if a contract row's non-Apple chord still says Cmd then broken
//   if a flag-gated row loses its flag id in the contract then broken

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { EXPORT_PATH, renderExport } from '../../scripts/export-hotkeys-contract.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let registry;
let flag;

before(async () => {
	registry = await loadTypeScriptModule('src/lib/components/rb/hotkeys/hotkeys-registry.ts');
	flag = await loadTypeScriptModule('src/lib/assistant/sidebar-flag.ts');
});

test('[if] the registry changes without rerunning the export [then] the committed contract is stale, [else stop].', async () => {
	assert.equal(
		readFileSync(EXPORT_PATH, 'utf8'),
		await renderExport(),
		'apps/opendj_cli/hotkeys_contract.json is stale: run node scripts/export-hotkeys-contract.mjs in apps/webui/frontend'
	);
});

test('[if] a row uses the primary modifier [then] the contract shows Cmd on mac and Ctrl elsewhere, [else stop].', () => {
	const [row] = registry.hotkeyContractRows([
		{ id: 'x', chord: 'Cmd+Shift+Z', description: 'Cmd+Z undoes', group: 'G' }
	]);
	assert.deepEqual(row.mac, { chord: 'Cmd+Shift+Z', description: 'Cmd+Z undoes' });
	assert.deepEqual(row.other, { chord: 'Ctrl+Shift+Z', description: 'Ctrl+Z undoes' });
	assert.equal(row.feature_flag, null);
});

test('[if] the assistant row is exported [then] it names the sidebar build flag the page reads, [else stop].', async () => {
	const doc = JSON.parse(await renderExport());
	const row = doc.rows.find((r) => r.id === 'assistant-toggle');
	assert.equal(row?.feature_flag, flag.ASSISTANT_SIDEBAR_FLAG_ID);
	assert.equal(row?.mac.chord, 'Cmd+I');
	assert.equal(row?.other.chord, 'Ctrl+I');
	assert.deepEqual(
		doc.rows.filter((r) => r.feature_flag !== null).map((r) => r.id),
		Object.keys(registry.HOTKEY_FEATURE_FLAGS)
	);
});
