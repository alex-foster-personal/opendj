// requirement: CUEOUT-26
// [if] Electron joins CoreAudio onto Chromium sink ids [then] speaker and jack share physical_id and keep browser ids, [else stop]
// [if] MAIN and CUE are the MacBook speaker/jack pair [then] split cue uses the Chromium headphone id, [else stop]
// [if] MAIN is the jack-muted speaker [then] assertMasterCanSound refuses it, [else stop]
// [if] labels are ambiguous [then] the join fails closed, [else stop]
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let io;
let headphones;
let collision;

const CHROMIUM_SPEAKER = 'chromium-speaker-id';
const CHROMIUM_HP = 'chromium-headphone-id';
const CHROMIUM_DEFAULT = 'default';
const CHROMIUM_OTHER = 'chromium-display-id';
const PHYSICAL_HP = 'coreaudio:BuiltInHeadphoneOutputDevice';

const CORE_AUDIO = [
	{
		uid: 'BuiltInSpeakerDevice',
		name: 'MacBook Pro Speakers',
		channels: 2,
		transport: 'builtin',
		is_default: false,
		muted_by_jack: true,
		physical_uid: 'BuiltInHeadphoneOutputDevice'
	},
	{
		uid: 'BuiltInHeadphoneOutputDevice',
		name: 'External Headphones',
		channels: 2,
		transport: 'builtin',
		is_default: true,
		muted_by_jack: false,
		physical_uid: 'BuiltInHeadphoneOutputDevice'
	},
	{
		uid: 'lg',
		name: 'LG ULTRAWIDE',
		channels: 2,
		transport: 'display',
		is_default: false,
		muted_by_jack: false,
		physical_uid: 'lg'
	}
];

before(async () => {
	io = await loadTypeScriptModule('src/lib/player/io-device-access.ts');
	headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
	collision = await loadTypeScriptModule('src/lib/player/main-cue-collision.ts');
});

function browserListing() {
	return [
		{ id: CHROMIUM_DEFAULT, label: 'System default output' },
		{ id: CHROMIUM_SPEAKER, label: 'MacBook Pro Speakers' },
		{ id: CHROMIUM_HP, label: 'External Headphones' },
		{ id: CHROMIUM_OTHER, label: 'LG ULTRAWIDE' }
	];
}

test('CoreAudio fields join onto Chromium rows without replacing browser ids', () => {
	const enriched = io.joinElectronCoreAudioOutputs(browserListing(), CORE_AUDIO);
	const speaker = enriched.find((row) => row.id === CHROMIUM_SPEAKER);
	const hp = enriched.find((row) => row.id === CHROMIUM_HP);
	assert.equal(speaker?.physical_id, PHYSICAL_HP);
	assert.equal(hp?.physical_id, PHYSICAL_HP);
	assert.equal(speaker?.muted_by_jack, true);
	assert.equal(hp?.muted_by_jack, false);
	assert.equal(speaker?.transport, 'builtin');
	const defaultRow = enriched.find((row) => row.id === CHROMIUM_DEFAULT);
	assert.equal(defaultRow?.physical_id, PHYSICAL_HP);
	assert.equal(defaultRow?.id, CHROMIUM_DEFAULT);
});

// PRRT_kwDOSEvNd86y2ORd: default alias from groupId must upgrade via concrete browser id, not CoreAudio fallback alone.
test('listIoDevices default alias inherits enriched CoreAudio fields from its concrete browser target', () => {
	const devices = [
		{
			kind: 'audiooutput',
			deviceId: CHROMIUM_DEFAULT,
			label: 'Default - External Headphones',
			groupId: 'grp-hp'
		},
		{
			kind: 'audiooutput',
			deviceId: CHROMIUM_SPEAKER,
			label: 'MacBook Pro Speakers',
			groupId: 'grp-spk'
		},
		{
			kind: 'audiooutput',
			deviceId: CHROMIUM_HP,
			label: 'External Headphones',
			groupId: 'grp-hp'
		},
		{ kind: 'audiooutput', deviceId: CHROMIUM_OTHER, label: 'LG ULTRAWIDE', groupId: 'grp-lg' }
	];
	const listing = io.listIoDevices(devices);
	const defaultBeforeJoin = listing.outputs.find((row) => row.id === CHROMIUM_DEFAULT);
	assert.equal(defaultBeforeJoin?.physical_id, CHROMIUM_HP);
	const enriched = io.joinElectronCoreAudioOutputs(listing.outputs, CORE_AUDIO);
	const defaultRow = enriched.find((row) => row.id === CHROMIUM_DEFAULT);
	assert.equal(defaultRow?.physical_id, PHYSICAL_HP);
	assert.equal(defaultRow?.muted_by_jack, false);
	assert.equal(defaultRow?.transport, 'builtin');
});

test('speaker plus jack refuses a CUE on the Chromium headphone id instead of auto-splitting (AUDFID-01)', () => {
	const enriched = io.joinElectronCoreAudioOutputs(browserListing(), CORE_AUDIO);
	const assignment = headphones.dualSinkAssignment({
		outputs: enriched,
		selectedCueId: CHROMIUM_HP,
		selectedMasterId: null
	});
	assert.equal(assignment.cueId, null);
	assert.equal(assignment.masterId, null);
	assert.match(assignment.cueRefusal, /MAIN and CUE are the same output/);
});

test('assertMasterCanSound refuses the muted Chromium speaker id', () => {
	const enriched = io.joinElectronCoreAudioOutputs(browserListing(), CORE_AUDIO);
	assert.throws(
		() => collision.assertMasterCanSound(CHROMIUM_SPEAKER, enriched),
		/muted while headphones are in the headphone jack/
	);
});

test('genuinely separate outputs keep two-output routing', () => {
	const enriched = io.joinElectronCoreAudioOutputs(browserListing(), CORE_AUDIO);
	assert.deepEqual(
		headphones.dualSinkAssignment({
			outputs: enriched,
			selectedCueId: CHROMIUM_HP,
			selectedMasterId: CHROMIUM_OTHER,
			currentRoomId: null
		}),
		{ masterId: CHROMIUM_OTHER, cueId: CHROMIUM_HP, boothId: null, autoPinnedMaster: false }
	);
});

test('duplicate browser labels fail closed instead of guessing', () => {
	const dup = [
		{ id: 'a', label: 'MacBook Pro Speakers' },
		{ id: 'b', label: 'MacBook Pro Speakers' },
		{ id: CHROMIUM_HP, label: 'External Headphones' }
	];
	assert.throws(
		() => io.joinElectronCoreAudioOutputs(dup, CORE_AUDIO),
		/no unique match for that label/
	);
});

test('a muted speaker with no jack target in CoreAudio fails explicitly', () => {
	const broken = [
		{
			uid: 'BuiltInSpeakerDevice',
			name: 'MacBook Pro Speakers',
			channels: 2,
			transport: 'builtin',
			is_default: false,
			muted_by_jack: true,
			physical_uid: 'MissingJackUid'
		}
	];
	const browser = [{ id: CHROMIUM_SPEAKER, label: 'MacBook Pro Speakers' }];
	assert.throws(
		() => io.joinElectronCoreAudioOutputs(browser, broken),
		/physical_uid MissingJackUid is missing/
	);
});

// PRRT_kwDOSEvNd86y2ORd: duplicate labels must not replace a known browser default alias.
test('duplicate USB labels keep the browser default alias on the concrete Chromium id', () => {
	const coreAudio = [
		{
			uid: 'usb-a',
			name: 'USB Audio',
			channels: 2,
			transport: 'usb',
			is_default: true,
			muted_by_jack: false,
			physical_uid: 'usb-a'
		},
		{
			uid: 'usb-b',
			name: 'USB Audio',
			channels: 2,
			transport: 'usb',
			is_default: false,
			muted_by_jack: false,
			physical_uid: 'usb-b'
		}
	];
	const devices = [
		{ kind: 'audiooutput', deviceId: 'default', label: 'Default - USB Audio', groupId: 'g-usb1' },
		{ kind: 'audiooutput', deviceId: 'usb1', label: 'USB Audio', groupId: 'g-usb1' },
		{ kind: 'audiooutput', deviceId: 'usb2', label: 'USB Audio', groupId: 'g-usb2' }
	];
	const listing = io.listIoDevices(devices);
	const enriched = io.joinElectronCoreAudioOutputs(listing.outputs, coreAudio);
	const defaultRow = enriched.find((row) => row.id === 'default');
	assert.equal(defaultRow?.physical_id, 'usb1');
	assert.equal(collision.sameOutputDevice(enriched, 'default', 'usb1'), true);
	assert.equal(collision.sameOutputDevice(enriched, 'default', 'usb2'), false);
});
