// requirement: CUEOUT-02
// [if] `split_cable` is active [then] L carries only master (mono sum) and R carries only cue (mono sum); a stereo test tone panned hard left on a cued deck with fader down is audible on R and silent on L
// [if] `split_cable` is active and no channel has CUE on [then] R is silent
// [if] the mode switches back to `practice` or `two_outputs` [then] the main output returns to stereo master within one parameter-smoothing window, with no click (setTargetAtTime, not a hard set)
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let headphones;

before(async () => {
	headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
});

test('splitCableGains routes master to left and cue to right in split_cable mode', () => {
	assert.deepEqual(headphones.splitCableGains('split_cable', 0, 1), {
		left: 1,
		rightCue: 1,
		rightMaster: 0
	});
	assert.deepEqual(headphones.splitCableGains('split_cable', 1, 1), {
		left: 1,
		rightCue: 0,
		rightMaster: 1
	});
	const center = headphones.splitCableGains('split_cable', 0.5, 1);
	assert.equal(center.left, 1);
	const mixCenter = headphones.headphoneMixGains(0.5);
	assert.equal(center.rightCue, mixCenter.cue);
	assert.equal(center.rightMaster, mixCenter.master);
});

test('splitCableGains scales the cue ear by GAIN and leaves the room left unscaled', () => {
	assert.deepEqual(headphones.splitCableGains('split_cable', 0, 0.25), {
		left: 1,
		rightCue: 0.25,
		rightMaster: 0
	});
	assert.deepEqual(headphones.splitCableGains('split_cable', 1, 0.5), {
		left: 1,
		rightCue: 0,
		rightMaster: 0.5
	});
	assert.throws(() => headphones.splitCableGains('split_cable', 0), /headphone level/);
});

test('splitCableGains is silent outside split_cable mode', () => {
	for (const mode of ['practice', 'two_outputs']) {
		assert.deepEqual(headphones.splitCableGains(mode, 0, 1), {
			left: 0,
			rightCue: 0,
			rightMaster: 0
		});
		assert.deepEqual(headphones.splitCableGains(mode, 1, 0.25), {
			left: 0,
			rightCue: 0,
			rightMaster: 0
		});
	}
});

test('practiceMainGains silences the stereo main path in split_cable mode', () => {
	assert.deepEqual(headphones.practiceMainGains('split_cable', null, 0.5), { cue: 0, master: 0 });
});

test('split_cable is a legal output_mode and unknown values still fail fast', () => {
	assert.doesNotThrow(() => headphones.assertHeadphoneOutputMode('split_cable'));
	assert.throws(() => headphones.assertHeadphoneOutputMode('nope'), /split_cable/);
	assert.deepEqual(headphones.HEADPHONE_OUTPUT_MODES, ['practice', 'two_outputs', 'split_cable']);
});

test('split-cable graph uses ChannelMerger L/R assignment and mono downmix', async () => {
	const [hpSrc, engineSrc] = await Promise.all([
		readFile('src/lib/player/headphones.ts', 'utf8'),
		readFile('src/lib/rb/audio-engine.svelte.ts', 'utf8')
	]);
	assert.match(hpSrc, /createChannelMerger\(2\)/);
	assert.match(hpSrc, /splitLeftGain\.connect\(nodes\.splitMerger, 0, 0\)/);
	assert.match(hpSrc, /splitRightCueGain\.connect\(nodes\.splitMerger, 0, 1\)/);
	assert.match(hpSrc, /splitRightMasterGain\.connect\(nodes\.splitMerger, 0, 1\)/);
	assert.match(hpSrc, /masterSplitter\.connect\(nodes\.masterLeftHalf, 0, 0\)/);
	assert.match(hpSrc, /masterSplitter\.connect\(nodes\.masterRightHalf, 1, 0\)/);
	assert.match(hpSrc, /cueSplitter\.connect\(nodes\.cueLeftHalf, 0, 0\)/);
	assert.match(hpSrc, /cueSplitter\.connect\(nodes\.cueRightHalf, 1, 0\)/);
	assert.match(hpSrc, /masterLeftHalf\.connect\(nodes\.masterMono\)/);
	assert.match(hpSrc, /masterRightHalf\.connect\(nodes\.masterMono\)/);
	assert.match(hpSrc, /cueLeftHalf\.connect\(nodes\.cueMono\)/);
	assert.match(hpSrc, /cueRightHalf\.connect\(nodes\.cueMono\)/);
	assert.match(hpSrc, /splitMerger\.connect\(muteGain\)/);
	assert.match(engineSrc, /wireSplitCableIntoMasterPath\(_masterGain, _masterMuteGain, headphones\)/);
	const wireStart = hpSrc.indexOf('export function wireSplitCableIntoMasterPath');
	const wireEnd = hpSrc.indexOf('\nfunction _headphoneError', wireStart);
	const wireBody = hpSrc.slice(wireStart, wireEnd === -1 ? undefined : wireEnd);
	assert.doesNotMatch(wireBody, /setSinkId|\.connect\(dest\)|createMediaStreamDestination/);
});

test('applyHeadphoneMix ramps split gains with setTargetAtTime, not hard gain.value', async () => {
	const hpSrc = await readFile('src/lib/player/headphones.ts', 'utf8');
	const applyStart = hpSrc.indexOf('export function applyHeadphoneMix');
	const applyEnd = hpSrc.indexOf('export function setHeadphoneOutputMode', applyStart);
	const applyBody = hpSrc.slice(applyStart, applyEnd === -1 ? undefined : applyEnd);
	assert.match(applyBody, /splitCableGains\(/);
	assert.match(applyBody, /nodes\.splitLeftGain\.gain/);
	assert.match(applyBody, /nodes\.splitRightCueGain\.gain/);
	assert.match(applyBody, /nodes\.splitRightMasterGain\.gain/);
	assert.match(applyBody, /_setMonitorParam/);
	assert.doesNotMatch(applyBody, /splitLeftGain\.gain\.value\s*=/);
	assert.doesNotMatch(applyBody, /splitRightCueGain\.gain\.value\s*=/);
	assert.doesNotMatch(applyBody, /splitRightMasterGain\.gain\.value\s*=/);
});

test('HeadphoneCluster exposes split-cable mode label, warning, and SPLIT control', async () => {
	const clusterSrc = await readFile('src/lib/components/rb/mixer/HeadphoneCluster.svelte', 'utf8');
	assert.match(clusterSrc, /data-output-mode=\{headphoneState\.output_mode\}/);
	assert.match(clusterSrc, /split cable/);
	assert.match(clusterSrc, /aria-pressed=\{headphoneState\.output_mode === 'split_cable'\}/);
	assert.match(clusterSrc, /Mono master on LEFT/);
	assert.match(clusterSrc, /DJ splitter cable\. A Y cable/);
	assert.match(clusterSrc, />SPLIT cable</);
});
