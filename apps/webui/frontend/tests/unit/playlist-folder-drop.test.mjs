// requirement: LIBUX-16
// [if] the same folder is dropped twice, or a failed drop is retried [then] each drop stages into a fresh batch, never one a prior drop already filled [else stop].
//
// The orchestration itself (playlist create, upload, materialize, membership)
// is exercised against the real engine and a real browser drop in
// tests/e2e/playlist-folder-drop.spec.ts, not against simulated fetch here.

import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

// Mirrors apps/webui/server/routes/ingest.py BATCH_RE, the server's own gate.
const SERVER_BATCH_RE = /^[A-Za-z0-9][A-Za-z0-9._ -]{0,79}$/;
const NOW = new Date(Date.UTC(2026, 8, 23, 14, 15, 3));

let folderDrop;

before(async () => {
	folderDrop = await loadTypeScriptModule('src/lib/rb/playlist-folder-drop.ts');
});

describe('batchNameFromFolder', () => {
	it('keeps a readable folder prefix plus a UTC second stamp and entropy', () => {
		const name = folderDrop.batchNameFromFolder('Agnes Obel', NOW);
		assert.match(name, /^Agnes Obel-20260923-141503-[0-9a-f]{8}$/);
		assert.match(name, SERVER_BATCH_RE);
	});

	it('never reuses a batch for the same folder, even within one second', () => {
		const names = new Set();
		for (let i = 0; i < 50; i += 1) {
			names.add(folderDrop.batchNameFromFolder('Agnes Obel', NOW));
		}
		assert.equal(names.size, 50);
	});

	it('falls back to a drop- prefix when nothing of the name survives sanitizing', () => {
		const name = folderDrop.batchNameFromFolder('!!!', NOW);
		assert.match(name, /^drop-20260923-141503-[0-9a-f]{8}$/);
	});

	it('sanitizes and truncates long or exotic names into a server-valid batch', () => {
		for (const folder of ['Björk / Homogenic (1997)', '...hidden', 'x'.repeat(200), ' . - ']) {
			const name = folderDrop.batchNameFromFolder(folder, NOW);
			assert.match(name, SERVER_BATCH_RE, `${folder} -> ${name}`);
			assert.ok(name.length <= 80, `${name.length} > 80`);
		}
	});
});
