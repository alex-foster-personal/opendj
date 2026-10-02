// requirement: CUEOUT-09
// [if] wired headphones become the OS default while cue is that device and master is unpinned [then] dualSinkAssignment pins master to a speaker-looking output, not the cue device
// [if] MASTER/MAIN is set to speakers and HEADPHONE CUE is set to headphones [then] the master sink id and the cue element sink id are those two different device ids
// [if] AudioContext.setSinkId is missing [then] headphone_master_select throws naming Chrome and the cue sink is left unchanged
// [if] the I/O accessible name is not SHOW AUDIO I/O [then] the CUEOUT-06 probe fails on the name lookup
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND = fileURLToPath(new URL('../..', import.meta.url));
const CLUSTER = `${FRONTEND}/src/lib/components/rb/mixer/HeadphoneCluster.svelte`;
const EXPLAINER = `${FRONTEND}/src/lib/components/rb/deck/ControlExplainer.svelte`;

let headphones;

before(async () => {
	headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
});

const WIRED = [
	{ id: 'lg', label: 'LG ULTRAWIDE' },
	{ id: 'hp', label: 'External Headphones' },
	{ id: 'speakers', label: 'MacBook Pro Speakers' }
];

test('outputLooksLikeHeadphones matches wired and Bluetooth phones, not speakers', () => {
	assert.equal(headphones.outputLooksLikeHeadphones('External Headphones'), true);
	assert.equal(headphones.outputLooksLikeHeadphones('AirPods G2'), true);
	assert.equal(headphones.outputLooksLikeHeadphones('MacBook Pro Speakers'), false);
	assert.equal(headphones.outputLooksLikeSpeakers('MacBook Pro Speakers'), true);
	assert.equal(headphones.outputLooksLikeSpeakers('External Headphones'), false);
});

test('preferredMasterOutputDeviceId prefers speakers and never the excluded cue device', () => {
	assert.equal(headphones.preferredMasterOutputDeviceId(WIRED, 'hp'), 'speakers');
	assert.equal(headphones.preferredMasterOutputDeviceId(WIRED, null), 'speakers');
	assert.equal(
		headphones.preferredMasterOutputDeviceId(
			[
				{ id: 'hp', label: 'External Headphones' },
				{ id: 'bt', label: 'AirPods' }
			],
			'hp'
		),
		null
	);
});

test('dualSinkAssignment auto-pins master to speakers when cue is headphones and master is unpinned', () => {
	assert.deepEqual(
		headphones.dualSinkAssignment({
			outputs: WIRED,
			selectedCueId: 'hp',
			selectedMasterId: null
		}),
		{ masterId: 'speakers', cueId: 'hp', autoPinnedMaster: true }
	);
	assert.deepEqual(
		headphones.dualSinkAssignment({
			outputs: WIRED,
			selectedCueId: 'hp',
			selectedMasterId: 'speakers'
		}),
		{ masterId: 'speakers', cueId: 'hp', autoPinnedMaster: false }
	);
	assert.deepEqual(
		headphones.dualSinkAssignment({
			outputs: WIRED,
			selectedCueId: 'hp',
			selectedMasterId: 'hp'
		}),
		{ masterId: 'hp', cueId: 'hp', autoPinnedMaster: false }
	);
});

test('audioContextSinkIdIsSupported is a function check, not a silent fallback', () => {
	assert.equal(headphones.audioContextSinkIdIsSupported({ setSinkId: async () => {} }), true);
	assert.equal(headphones.audioContextSinkIdIsSupported({}), false);
});

test('master setSinkId failure names Chrome and does not silently follow the OS default', () => {
	const source = readFileSync(`${FRONTEND}/src/lib/player/headphones.ts`, 'utf8');
	assert.match(source, /AudioContext\.setSinkId is unavailable/);
	assert.match(source, /Use Chrome for two-device cue/);
});

test('HeadphoneCluster I/O menu names MASTER, HEADPHONE CUE, AUDIO IN and keeps SHOW AUDIO I/O', () => {
	const source = readFileSync(CLUSTER, 'utf8');
	assert.match(source, /aria-label="SHOW AUDIO I\/O"/);
	assert.match(source, /><span>SET<\/span><span>OUTPUTS<\/span></);
	assert.match(source, /ControlExplainer/);
	assert.match(source, /title="MIX"/);
	assert.match(source, /title="GAIN"/);
	assert.match(source, /label="GAIN"/);
	assert.doesNotMatch(source, /title="LEVEL"/);
	assert.doesNotMatch(source, /label="LEVEL"/);
	assert.match(source, /title="MAIN"/);
	assert.match(source, /title="SPLIT"/);
	assert.match(source, /title="HEAD DELAY"/);
	assert.match(source, /title="Audio I\/O"/);
	assert.match(source, /title="Output mode"/);
	assert.match(source, /title="Pinned sinks"/);
	assert.match(source, /title="SPLIT warning"/);
	assert.match(source, /title="Two outputs warning"/);
	assert.match(source, /title="Rescan"/);
	assert.match(source, /title="MASTER \/ MAIN"/);
	assert.match(source, /title="HEADPHONE CUE"/);
	assert.match(source, /title="AUDIO IN"/);
	assert.match(source, /MASTER \/ MAIN/);
	assert.match(source, /HEADPHONE CUE/);
	assert.match(source, /AUDIO IN/);
	assert.match(source, /aria-label="master output device"/);
	assert.match(source, /aria-label="headphone output device"/);
	assert.match(source, /aria-label="audio input device"/);
	assert.doesNotMatch(source, /title="Pinned master and cue sinks"/);
	assert.doesNotMatch(source, /title="Split-cable mode requires/);
	assert.match(source, /pinOnClick/);
	assert.match(source, /resetValue=\{0\}/);
	assert.match(source, /headphoneMixAccent/);
	assert.match(source, /Press CALIBRATE afterwards/, 'CUEOUT-14: the cue pick explains the modal, not the removed first-select chirp');
	assert.match(source, /cannot be interrupted/);
	assert.match(source, /CUEOUT-12/);
	assert.doesNotMatch(source, />\+ OUT</);
	assert.doesNotMatch(source, /ADD OUTPUT/);
	assert.doesNotMatch(source, /\+ OUT briefly/);
});

test('generated headphone POST operations are aliases, not 35-line clones', () => {
	const text = readFileSync(`${FRONTEND}/src/lib/api-types.ts`, 'utf8');
	assert.match(text, /type _HeadphoneJsonPost =/);
	assert.match(
		text,
		/post_headphone_mix_api_v1_performance_headphones_mix_post: _HeadphoneJsonPost;/
	);
	assert.match(
		text,
		/post_headphone_master_select_api_v1_performance_headphones_outputs_master_post: _HeadphoneJsonPost;/
	);
	assert.match(
		text,
		/post_headphone_input_select_api_v1_performance_headphones_inputs_select_post: _HeadphoneJsonPost;/
	);
	assert.doesNotMatch(
		text,
		/post_headphone_mix_api_v1_performance_headphones_mix_post: \{/
	);
	assert.doesNotMatch(text, /_HeadphoneJsonPost;;/);
});

test('ControlExplainer remasures a tall action pop so it cannot cover I/O', () => {
	const source = readFileSync(EXPLAINER, 'utf8');
	assert.match(source, /placeFloating/);
	assert.match(source, /offsetHeight/);
	assert.match(source, /width: 240, height: 120/);
	assert.match(source, /width: 280, height: 320/);
	assert.match(source, /has-action/);
});

test('pinned I/O explainer closes on outside click, not only while a SELECT is focused', () => {
	const source = readFileSync(EXPLAINER, 'utf8');
	assert.match(source, /function _onDocumentPointerDown/);
	assert.match(source, /popEl\?\.contains\(target\)/);
	assert.match(source, /HTMLSelectElement/);
	assert.match(source, /document\.documentElement/);
	assert.doesNotMatch(source, /document\.activeElement\?\.tagName === 'SELECT'/);
});
