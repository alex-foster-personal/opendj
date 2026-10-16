/**
 * PERFMODE-11's boot-stamp mirror must never write a prefs blob that the
 * prefs loader rejects.
 *
 * ORIGIN (Fri 9 Oct 2026, the Electron shell gate, ELECTRON-13). A fresh
 * Electron profile over an engine that already held `app_mode.last_gig_at`:
 * prefs hydration mirrored the stamp into an EMPTY localStorage, creating
 * `{"app_mode_last_gig_at": ...}` under `mdt.rb.ui-prefs.v1`. The next load
 * threw "malformed prefs blob (hide_broken_links is not a boolean)" from
 * prefs.svelte.ts `_load` and the app never mounted.
 *
 * [if] no prefs blob exists [then] the mirror writes nothing
 * [if] a prefs blob exists [then] the stamp lands in it and every other key
 *   survives (the mirror still works; an overshoot that never writes would
 *   pass the first case and break PERFMODE-11's cold-boot read)
 */
import assert from 'node:assert/strict';
import { before, beforeEach, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const KEY = 'mdt.rb.ui-prefs.v1';

class MemoryStorage {
	#items = new Map();
	getItem(key) {
		return this.#items.has(key) ? this.#items.get(key) : null;
	}
	setItem(key, value) {
		this.#items.set(key, String(value));
	}
	removeItem(key) {
		this.#items.delete(key);
	}
}

describe('boot-stamp mirror', () => {
	let mirror;
	before(async () => {
		mirror = await loadTypeScriptModule('src/lib/rb/gig-stamp-mirror.ts');
	});
	beforeEach(() => {
		globalThis.localStorage = new MemoryStorage();
	});

	it('writes nothing when no prefs blob exists yet', () => {
		mirror.writeBootStampMirror('2026-10-09T00:00:00.000Z');
		assert.equal(globalThis.localStorage.getItem(KEY), null);
		assert.equal(mirror.readBootStampMirror(), null);
	});

	it('mirrors into an existing prefs blob and keeps its keys', () => {
		globalThis.localStorage.setItem(KEY, JSON.stringify({ hide_broken_links: false }));
		mirror.writeBootStampMirror('2026-10-09T00:00:00.000Z');
		assert.deepEqual(JSON.parse(globalThis.localStorage.getItem(KEY)), {
			hide_broken_links: false,
			app_mode_last_gig_at: '2026-10-09T00:00:00.000Z'
		});
		assert.equal(mirror.readBootStampMirror(), '2026-10-09T00:00:00.000Z');
	});
});
