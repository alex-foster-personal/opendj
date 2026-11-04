// requirement: AUDFID-05 (restore the saved MAIN and CUE at launch)
// [if] the saved MAIN and CUE are both listed at launch [then] two_outputs is restored on exactly those devices with a "Restored: MAIN X, CUE Y" pill and Undo ⛔️
// [if] one saved device is missing [then] the app starts in practice mode with a message naming it ⛔️
// [if] the device list is withheld [then] nothing is restored and no input is opened to read it ⛔️
// [if] the restore runs [then] it opens no input, and it runs once per session ⛔️
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let hp;
let refresh;
before(async () => {
	hp = await loadTypeScriptModule('src/lib/player/headphones.ts');
	refresh = await loadTypeScriptModule('src/lib/rb/io-device-refresh.ts');
});

const MAIN = { id: 'native:hyperboom', label: 'HYPERBOOM' };
const CUE = { id: 'native:sony', label: 'af-sony-fun' };
const OUTPUTS = [MAIN, CUE, { id: 'native:spk', label: 'MacBook Pro Speakers' }];

test('if both saved devices are listed and the plan is not restore then a restart drops the cue to practice', () => {
	assert.deepEqual(hp.launchOutputRestorePlan({ saved: { master: MAIN, cue: CUE }, outputs: OUTPUTS, listed: true }), {
		kind: 'restore',
		master: MAIN,
		cue: CUE
	});
	assert.equal(hp.launchRestoredText('HYPERBOOM', 'af-sony-fun'), 'Restored: MAIN HYPERBOOM, CUE af-sony-fun');
});

test('if a missing saved device is not named then the operator cannot tell why the cue is off', () => {
	const plan = hp.launchOutputRestorePlan({ saved: { master: MAIN, cue: CUE }, outputs: [MAIN], listed: true });
	assert.deepEqual(plan, { kind: 'practice', missing: [CUE] });
	assert.equal(hp.launchMissingText(plan.missing), 'Started in practice mode: saved device af-sony-fun is not connected.');
	const both = hp.launchOutputRestorePlan({ saved: { master: MAIN, cue: CUE }, outputs: [], listed: true });
	assert.match(hp.launchMissingText(both.missing), /devices HYPERBOOM and af-sony-fun are not connected/);
});

test('if a withheld listing or a half-saved pair restores anything then launch guesses routes', () => {
	assert.equal(hp.launchOutputRestorePlan({ saved: { master: MAIN, cue: CUE }, outputs: OUTPUTS, listed: false }).kind, 'skip');
	assert.equal(hp.launchOutputRestorePlan({ saved: { master: MAIN, cue: null }, outputs: OUTPUTS, listed: true }).kind, 'skip');
	assert.equal(hp.launchOutputRestorePlan({ saved: { master: null, cue: null }, outputs: OUTPUTS, listed: true }).kind, 'skip');
});

test('if the launch restore opens an input or runs twice then IOPIN-14 is broken or a remount re-routes the set', () => {
	const source = readFileSync(new URL('../../src/lib/player/headphones.ts', import.meta.url), 'utf8');
	const body = source.slice(source.indexOf('export async function restoreSavedOutputsAtLaunch('), source.indexOf('export async function undoLaunchOutputRestore('));
	for (const opener of ['getUserMedia', '_unlockWebviewInputs', '_unlockHeadphoneOutputLabels', 'requestIoDeviceNames', 'startInputLevelWatch']) {
		assert.ok(!body.includes(opener), `the launch restore reaches ${opener}`);
	}
	assert.match(body, /if \(_launchRestoreRan\) return;\n\t_launchRestoreRan = true;/);
	assert.match(body, /await selectMasterOutput\(plan\.master\.id, monitorSource\);\n\t\t\t\tawait selectHeadphoneOutput\(plan\.cue\.id, monitorSource\);/);
	assert.match(body, /label: 'Undo',\n\t\t\t\thandler: \(\) => void undoLaunchOutputRestore\(monitorSource\)/);
});

test('if the mount lists without then restoring, or restores on the Rust engine, then restarts lose the outputs or fail loudly', async () => {
	const calls = [];
	const deps = {
		rustUnsupported: () => false,
		refresh: async () => calls.push('refresh'),
		request: async () => calls.push('request'),
		attempts: () => calls.length,
		publishFailure: () => calls.push('failure')
	};
	await refresh.listIoDevicesAndRestoreOutputsOnMount(deps, async () => calls.push('restore'));
	assert.deepEqual(calls, ['refresh', 'restore']);
	const rust = [];
	await refresh.listIoDevicesAndRestoreOutputsOnMount(
		{ ...deps, rustUnsupported: () => true, publishFailure: () => rust.push('failure') },
		async () => rust.push('restore')
	);
	assert.deepEqual(rust, ['failure']);
	const mixer = readFileSync(new URL('../../src/lib/components/rb/Mixer.svelte', import.meta.url), 'utf8');
	assert.match(mixer, /onMount\(\(\) => void listIoDevicesAndRestoreOutputsOnMount\(\)\);/);
});

test('if agents cannot read the launch restore then the pill has no agent parity', () => {
	const server = readFileSync(new URL('../../../server/routes/performance_headphones.py', import.meta.url), 'utf8');
	assert.match(server, /launch_restore: dict\[str, Any\] \| None = None/);
	const types = readFileSync(new URL('../../src/lib/rb/mixer-types.ts', import.meta.url), 'utf8');
	assert.match(types, /status: 'restored' \| 'practice' \| 'skipped' \| 'failed' \| 'undone';/);
});
