// requirement: CUEOUT-01
// [if] no monitor device is selected and a channel's CUE is on [then] turning MIX toward cue makes that channel audible pre-fader in the main output, with master attenuated by the equal-power law
// [if] no monitor device is selected and no channel has CUE on [then] MIX at full cue produces silence in the main output rather than passing master through
// [if] a monitor device is selected [then] the main output is master-only again and MIX affects only the monitor, no cue bleed into the room
// [if] output_mode is set outside the enum via IPC [then] the command throws and state is unchanged
// [if] Bluetooth headphones are the sole output in practice mode [then] cue and master stay sample-aligned (same path); the mode never routes cue to a second device
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let headphones;

before(async () => {
	headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
});

test('practice mix reuses equal-power law and silences master at full cue', () => {
	assert.deepEqual(headphones.practiceMainGains('practice', null, 0), { cue: 1, master: 0 });
	assert.deepEqual(headphones.practiceMainGains('practice', null, 1), { cue: 0, master: 1 });
	const center = headphones.practiceMainGains('practice', null, 0.5);
	assert.ok(Math.abs(center.cue - Math.SQRT1_2) < 1e-12);
	assert.ok(Math.abs(center.master - Math.SQRT1_2) < 1e-12);
	assert.deepEqual(center, headphones.headphoneMixGains(0.5));
});

test('two_outputs and a selected monitor restore master-only main, including MIX at full cue', () => {
	assert.deepEqual(headphones.practiceMainGains('two_outputs', null, 0), { cue: 0, master: 1 });
	assert.deepEqual(headphones.practiceMainGains('two_outputs', 'usb-hp', 0), {
		cue: 0,
		master: 1
	});
	assert.deepEqual(headphones.practiceMainGains('practice', 'usb-hp', 0), {
		cue: 0,
		master: 1
	});
	assert.deepEqual(headphones.practiceMainGains('practice', 'bt-headphones', 0.5), {
		cue: 0,
		master: 1
	});
});

test('practiceCueWithGain scales cue only; full master stays at 1 regardless of GAIN', () => {
	assert.equal(headphones.practiceCueWithGain(1, 0.4), 0.4);
	assert.equal(headphones.practiceCueWithGain(0, 0.4), 0);
	assert.equal(headphones.practiceCueWithGain(Math.SQRT1_2, 1), Math.SQRT1_2);
	assert.throws(() => headphones.practiceCueWithGain(1, 1.2), /headphone level/);
	const practiceFullMaster = headphones.practiceMainGains('practice', null, 1);
	assert.deepEqual(practiceFullMaster, { cue: 0, master: 1 });
	assert.equal(headphones.practiceCueWithGain(practiceFullMaster.cue, 0), 0);
});

test('unknown output_mode values fail fast', () => {
	assert.throws(() => headphones.assertHeadphoneOutputMode('nope'), /split_cable/);
	assert.doesNotThrow(() => headphones.assertHeadphoneOutputMode('practice'));
	assert.doesNotThrow(() => headphones.assertHeadphoneOutputMode('two_outputs'));
	assert.doesNotThrow(() => headphones.assertHeadphoneOutputMode('split_cable'));
	assert.deepEqual(headphones.HEADPHONE_OUTPUT_MODES, ['practice', 'two_outputs', 'split_cable']);
	assert.deepEqual(headphones.practiceMainGains('split_cable', null, 0.5), { cue: 0, master: 0 });
});

test('practice blend sits on the master mute path, never a second sink', async () => {
	const [hpSrc, engineSrc, clusterSrc, typesSrc] = await Promise.all([
		readFile('src/lib/player/headphones.ts', 'utf8'),
		readFile('src/lib/rb/audio-engine.svelte.ts', 'utf8'),
		readFile('src/lib/components/rb/mixer/HeadphoneCluster.svelte', 'utf8'),
		readFile('src/lib/rb/mixer-types.ts', 'utf8')
	]);
	assert.match(hpSrc, /practiceCueMix\.connect\(muteGain\)/);
	assert.match(hpSrc, /practiceMasterMix\.connect\(muteGain\)/);
	assert.match(hpSrc, /cueSum\.connect\(nodes\.practiceCueMix\)/);
	assert.match(hpSrc, /masterGain\.connect\(nodes\.practiceMasterMix\)/);
	assert.match(engineSrc, /wirePracticeBlendIntoMasterPath\(_masterGain, _masterMuteGain, headphones\)/);
	assert.match(hpSrc, /output_mode = 'two_outputs'/);
	assert.match(hpSrc, /'practice'/);
	assert.match(hpSrc, /output_mode = mode/);
	assert.doesNotMatch(
		hpSrc,
		/output_mode = 'practice'/,
		'vanishing CUE must not fall back to practice MIX or the room gets dumped'
	);
	assert.match(hpSrc, /practiceMainGains\(/);
	assert.match(hpSrc, /nodes\.practiceCueMix\.gain/);
	assert.match(hpSrc, /nodes\.practiceMasterMix\.gain/);
	assert.match(hpSrc, /practiceCueWithGain\(practice\.cue, level\)/);
	assert.match(hpSrc, /splitCableGains\([^)]*level\)/);
	assert.match(typesSrc, /split_cable/);
	assert.match(typesSrc, /output_mode: HeadphoneOutputMode/);
	assert.match(clusterSrc, /data-output-mode=\{state\.output_mode\}/);
	assert.match(clusterSrc, /practice/);
	assert.match(clusterSrc, /two outputs/);
	assert.match(clusterSrc, />MAIN</);
	const wireStart = hpSrc.indexOf('export function wirePracticeBlendIntoMasterPath');
	const wireEnd = hpSrc.indexOf('\nfunction _headphoneError', wireStart);
	const wireBody = hpSrc.slice(wireStart, wireEnd === -1 ? undefined : wireEnd);
	assert.doesNotMatch(
		wireBody,
		/setSinkId|MediaStreamAudioDestinationNode|createMediaStreamDestination/
	);
});
