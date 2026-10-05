/**
 * LIBM-167: the Broken checkbox and the grey row share one predicate.
 * Unticked Broken (hideBroken true) hides an unavailable row and keeps a
 * present one. The cloud icon title names the probed file path.
 *
 * [if] Broken is unticked [then] an unavailable row is hidden and a present
 *   row stays [else stop]
 * [if] the cloud icon is hovered on an unavailable row [then] the title
 *   contains the probed file path and the reason [else stop]
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const PATH = '/music/missing/track.wav';

let contract;
let cloud;
before(async () => {
	contract = await loadTypeScriptModule('src/lib/components/rb/browser/pane-contract.svelte.ts');
	cloud = await loadTypeScriptModule('src/lib/components/rb/browser/track-cloud-state.ts');
});

const present = {
	stable_id: 'present-1',
	title: 'Here',
	artist: 'A',
	file_exists: true,
	file_availability: 'present',
	file_path: '/music/here.wav'
};
const absent = {
	stable_id: 'absent-1',
	title: 'Gone',
	artist: 'B',
	file_exists: false,
	file_availability: 'absent',
	file_path: PATH
};

describe('Broken filter uses the unavailable predicate', () => {
	it('hides an unavailable row and keeps a present row', () => {
		const hidden = contract.filterRows([present, absent], '', true);
		assert.deepEqual(hidden.map((r) => r.stable_id), ['present-1']);
		const shown = contract.filterRows([present, absent], '', false);
		assert.deepEqual(shown.map((r) => r.stable_id), ['present-1', 'absent-1']);
	});
});

describe('unavailable cloud icon hover', () => {
	it('title contains the file path and the reason', () => {
		const view = cloud.trackCloudView({
			fileExists: false,
			fileAvailability: 'absent',
			filePath: PATH,
			hasRemoteCopy: false,
			isStreaming: false,
			spotifyPending: false,
			transfer: null
		});
		assert.match(view.title, new RegExp(PATH.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
		assert.match(view.title, /missing on this machine/);
	});
});
