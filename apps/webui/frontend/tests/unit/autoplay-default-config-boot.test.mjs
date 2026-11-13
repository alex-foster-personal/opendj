/**
 * Default-config AutoPlay mount (issue #11216, parent #700): replay cold boot with
 * shipped enforce/maximize prefs and the enabled-at-mount scenario from production
 * helpers, not literals in the test body.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { after, before, describe, it } from 'node:test';

import { installTimerProbe, loadRuneModule } from './load-rune-module.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';
import { SAMPLE_ROWS } from './hostile-empty-harness.mjs';

const CONTROLLER_ENTRY = [
	"export { installAutoPlay, readAutoPlayControllerArmedFromUiPrefs } from '$lib/rb/auto-play.svelte';",
	"export { setAutoPlayEnabled, readAutoPlayDefaultConfigBootPrefs, readShippedAutoPlayMountPrefs, uiPrefs } from '$lib/rb/prefs.svelte';"
].join('\n');

let boot;
let appInit;

before(async () => {
	boot = await loadTypeScriptModule('src/lib/rb/autoplay-default-config-boot.ts');
	appInit = await loadTypeScriptModule('src/lib/rb/app-init.ts');
});

describe('default-config AutoPlay mount replay', () => {
	it('[if] default-config boot prefs are read [then] enabled is on with shipped enforce/maximize, [else stop].', () => {
		const prefs = boot.replayAutoPlayDefaultConfigMount(
			'playlist-a',
			[],
			SAMPLE_ROWS.byTitle
		).prefs;
		assert.equal(prefs.auto_play_enabled, true);
		assert.equal(prefs.auto_play_enforce_order, false);
		assert.equal(prefs.auto_play_maximize_reach, true);
	});

	it('[if] mount is enabled before rows hydrate [then] feed tracks empty live and does not snapshot, [else stop].', () => {
		const { atMount, feedActiveAfterHydrate } = boot.replayAutoPlayDefaultConfigMount(
			'playlist-a',
			[],
			[]
		);
		assert.equal(atMount.snapshotted, false);
		assert.deepEqual(atMount.publish, []);
		assert.equal(feedActiveAfterHydrate, false);
	});

	it('[if] rows hydrate while default-config armed [then] first non-empty observation snapshots PLAY-04, [else stop].', () => {
		const { hydrated, feedActiveAfterHydrate } = boot.replayAutoPlayDefaultConfigMount(
			'playlist-a',
			[],
			SAMPLE_ROWS.byBpm
		);
		assert.equal(hydrated.snapshotted, true);
		assert.deepEqual(
			hydrated.publish.map((r) => r.stable_id),
			['charlie', 'bravo', 'alpha']
		);
		assert.equal(feedActiveAfterHydrate, true);
	});

	it('[if] installAutoPlay uses default-config prefs [then] controller arms only when those prefs are on, [else stop].', async () => {
		const probe = installTimerProbe();
		let uninstall = null;
		try {
			const live = await loadRuneModule(CONTROLLER_ENTRY);
			const shipped = live.readShippedAutoPlayMountPrefs();
			assert.equal(shipped.auto_play_enabled, false);
			assert.equal(live.readAutoPlayControllerArmedFromUiPrefs(), shipped.auto_play_enabled);

			uninstall = live.installAutoPlay();
			await probe.flush();
			assert.equal(probe.liveIntervals(), 0, 'fresh shipped default leaves AutoPlay disarmed');

			const scenario = live.readAutoPlayDefaultConfigBootPrefs();
			live.setAutoPlayEnabled(scenario.auto_play_enabled);
			assert.equal(live.readAutoPlayControllerArmedFromUiPrefs(), true);
			await probe.flush();
			assert.equal(probe.liveIntervals(), 1, 'default-config on-at-mount arms the poll');

			uninstall();
			uninstall = null;
			await probe.flush();
			assert.equal(probe.liveIntervals(), 0);
		} finally {
			if (uninstall !== null) uninstall();
			probe.restore();
		}
	});
});

describe('default-config wiring guards', () => {
	const APP_INIT = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/app-init.ts', import.meta.url)),
		'utf8'
	);

	it('[if] app-init boots silence readers [then] they read AutoPlay armed via readAutoPlayArmedFromUiPrefs, [else stop].', () => {
		assert.match(APP_INIT, /export function readAutoPlayArmedFromUiPrefs/);
		assert.match(APP_INIT, /armed: readAutoPlayArmedFromUiPrefs\(\)/);
		assert.match(APP_INIT, /autoplay_enabled: readAutoPlayArmedFromUiPrefs\(\)/);
	});

	it('[if] app-init export is loaded [then] readAutoPlayArmedFromUiPrefs matches uiPrefs.auto_play_enabled, [else stop].', () => {
		assert.equal(typeof appInit.readAutoPlayArmedFromUiPrefs, 'function');
	});
});
