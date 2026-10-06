// requirement: CUEOUT-27
// [if] the picked output id is "default" [then] setSinkId receives "" (the user agent default, no permission needed)
// [if] the picked output id is a real device id [then] setSinkId receives it unchanged
// [if] a cue sink change to "default" succeeds [then] the holder still records "default", the id the UI lists
// [if] a failed cue change restores a previous "default" [then] the restore also sends ""
// [if] the master sink is applied through the browser [then] its setSinkId goes through the same mapping
// [if] the browser's default entry shares a groupId with a listed device [then] that device is its physical_id, so MAIN and CUE on the pair run as split cue, not two outputs
// [if] the default entry has no groupId match [then] it stays its own output
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/** A cue context that behaves like Chrome without mic permission: the
 * "default" pseudo-id is not found, "" and real ids land. */
function chromeWithoutPermission({ failResume = false } = {}) {
	const calls = [];
	return {
		calls,
		state: 'suspended',
		sinkId: 'old-phones',
		async setSinkId(id) {
			calls.push(`sink:${id}`);
			if (id === 'default') {
				const error = new Error('device default is not found');
				error.name = 'NotFoundError';
				throw error;
			}
			this.sinkId = id;
		},
		async resume() {
			calls.push('resume');
			if (failResume) throw new Error('resume refused');
			this.state = 'running';
		},
		async suspend() {
			calls.push('suspend');
			this.state = 'suspended';
		}
	};
}

test('"default" is sent to setSinkId as the empty string, real ids pass through', async () => {
	const headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
	assert.equal(headphones.browserSinkId('default'), '');
	assert.equal(headphones.browserSinkId('bt-1'), 'bt-1');
	assert.equal(headphones.browserSinkId(''), '');
});

test('picking the default output for cue lands without mic permission', async () => {
	const headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
	const ctx = chromeWithoutPermission();
	const holder = { cueDeviceId: 'old-phones' };
	await headphones.applyCueSinkTransaction(ctx, 'default', holder);
	assert.deepEqual(ctx.calls, ['sink:', 'resume'],
		'if "default" reaches setSinkId then a browser without mic permission throws NotFoundError and toasts - broken');
	assert.equal(ctx.state, 'running');
	assert.equal(holder.cueDeviceId, 'default', 'the holder keeps the id the device list shows');
});

test('restoring a previous default cue output also sends the empty string', async () => {
	const headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
	const ctx = chromeWithoutPermission({ failResume: true });
	const holder = { cueDeviceId: 'default' };
	await assert.rejects(headphones.applyCueSinkTransaction(ctx, 'bt-1', holder), /resume refused/);
	assert.deepEqual(ctx.calls, ['sink:bt-1', 'resume', 'sink:'],
		'if the restore sends "default" then it fails and the cue context is silenced instead of restored - broken');
	assert.equal(holder.cueDeviceId, 'default');
});

test('the browser master sink goes through the same mapping', () => {
	const source = readFileSync(new URL('../../src/lib/player/headphones.ts', import.meta.url), 'utf8');
	assert.match(source, /'master setSinkId', ctx\.setSinkId\(browserSinkId\(deviceId\)\)/,
		'if the master path calls setSinkId with the raw id then picking the default MAIN fails without mic permission - broken');
	assert.doesNotMatch(source, /\.setSinkId\((deviceId|previousId)\)/,
		'every browser setSinkId call maps the id first');
});

/** Chrome with permission: the `default` alias carries the groupId of the device it follows. */
const CHROME_LISTING = [
	{ kind: 'audiooutput', deviceId: 'default', label: 'Default - MacBook Pro Speakers', groupId: 'g-speakers' },
	{ kind: 'audiooutput', deviceId: 'spk-1', label: 'MacBook Pro Speakers', groupId: 'g-speakers' },
	{ kind: 'audiooutput', deviceId: 'usb-1', label: 'USB Interface', groupId: 'g-usb' }
];

test('the default alias names the device it follows as its physical output', async () => {
	const access = await loadTypeScriptModule('src/lib/player/io-device-access.ts');
	const outputs = access.listIoDevices(CHROME_LISTING).outputs;
	assert.equal(outputs.find((output) => output.id === 'default').physical_id, 'spk-1');
	assert.equal(outputs.find((output) => output.id === 'spk-1').physical_id, undefined);
	const noGroup = access.listIoDevices(CHROME_LISTING.map(({ groupId, ...device }) => device)).outputs;
	assert.equal(noGroup.find((output) => output.id === 'default').physical_id, undefined,
		'without a groupId match the alias stays its own output');
});

test('CUE on the default alias of the MAIN device runs split cue, not two outputs on one speaker', async () => {
	const access = await loadTypeScriptModule('src/lib/player/io-device-access.ts');
	const headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
	const outputs = access.listIoDevices(CHROME_LISTING).outputs;
	const cueOnAlias = headphones.dualSinkAssignment({ outputs, selectedCueId: 'default', selectedMasterId: 'spk-1', currentRoomId: null });
	assert.equal(cueOnAlias.splitSameDevice, true,
		'if "default" and the speakers read as two outputs then the cue mix plays in the room - broken');
	const mainOnAlias = headphones.dualSinkAssignment({ outputs, selectedCueId: 'spk-1', selectedMasterId: 'default', currentRoomId: null });
	assert.equal(mainOnAlias.splitSameDevice, true);
	const distinct = headphones.dualSinkAssignment({ outputs, selectedCueId: 'usb-1', selectedMasterId: 'default', currentRoomId: null });
	assert.notEqual(distinct.splitSameDevice, true, 'a genuinely different device keeps two outputs');
});
