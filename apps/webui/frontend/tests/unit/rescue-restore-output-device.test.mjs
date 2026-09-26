// requirement: RESCUE-05
// [if] the saved sink id is enumerated [then] it is selected by id, even when another output carries the saved label
// [if] the saved id is gone but exactly one output carries the saved label [then] that output is selected by label
// [if] neither id nor label resolves [then] no select is dispatched, the toast names the saved output AND the one actually in effect, restore continues
// [if] two outputs carry the saved label [then] nothing is guessed: the toast names the ambiguity
// [if] a sink select is refused [then] only that sink is reported refused; the other sink and every deck still restore
// [if] the bifrost1 ring (legacy snapshot, id only, new origin) is restored [then] nothing throws; master reported not_found
// [if] a snapshot is captured with a selected sink [then] the enumerated label is saved beside the id

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const resolveMod = await loadTypeScriptModule('src/lib/player/output-device-resolve.ts');
const snapshotMod = await loadTypeScriptModule('src/lib/rb/rescue-snapshot.ts');
const restoreMod = await loadTypeScriptModule('src/lib/rb/rescue-restore.svelte.ts');
const headphonesMod = await loadTypeScriptModule('src/lib/player/headphones.ts');

// The five audiooutput LABELS the installed app enumerated on bifrost1 (WebView2 153,
// gate-bifrost1-41623ef6a-run1 webview2-probe). The probe records labels only, so the
// ids are placeholders, except the literal Chromium pseudo-device ids.
const BF1_OUTPUTS = [
	{ id: 'default', label: 'Default - Headphones (Realtek(R) Audio)' },
	{ id: 'communications', label: 'Communications - LS32D70xE (NVIDIA High Definition Audio)' },
	{ id: 'a11ce0000speakers', label: 'Speakers (Realtek(R) Audio)' },
	{ id: 'b0b000000phones', label: 'Headphones (Realtek(R) Audio)' },
	{ id: 'c0ffee000monitor', label: 'LS32D70xE (NVIDIA High Definition Audio)' }
];
// The id the bifrost1 Crash Rescue ring captured under the PREVIOUS launch's origin.
const BF1_RING_MASTER_ID = 'c30eed5ed1658b363f1b5f2906393ba5075eee28e6b12486bfb420c272e0e7f5';

//------------------------------------------------------------------ resolver

test('resolver: an enumerated id resolves by id', () => {
	const result = resolveMod.resolveSavedOutputDevice(
		{ device_id: 'b0b000000phones', label: 'Headphones (Realtek(R) Audio)' },
		BF1_OUTPUTS
	);
	assert.deepEqual(result, { status: 'matched_id', device_id: 'b0b000000phones' });
});

test('resolver: id wins over a DIFFERENT output carrying the saved label (no label preference overshoot)', () => {
	const outputs = [
		{ id: 'old-id', label: 'Renamed By OS' },
		{ id: 'other-id', label: 'Speakers (Realtek(R) Audio)' }
	];
	const result = resolveMod.resolveSavedOutputDevice(
		{ device_id: 'old-id', label: 'Speakers (Realtek(R) Audio)' },
		outputs
	);
	assert.deepEqual(result, { status: 'matched_id', device_id: 'old-id' });
});

test('resolver: a salted-away id resolves by exact label to the new id', () => {
	const result = resolveMod.resolveSavedOutputDevice(
		{ device_id: BF1_RING_MASTER_ID, label: 'Speakers (Realtek(R) Audio)' },
		BF1_OUTPUTS
	);
	assert.deepEqual(result, { status: 'matched_label', device_id: 'a11ce0000speakers' });
});

test('resolver: label match is exact, so "Default - X" never stands in for "X"', () => {
	const outputs = [{ id: 'default', label: 'Default - Headphones (Realtek(R) Audio)' }];
	const result = resolveMod.resolveSavedOutputDevice(
		{ device_id: 'gone', label: 'Headphones (Realtek(R) Audio)' },
		outputs
	);
	assert.deepEqual(result, { status: 'not_found' });
});

test('resolver: neither id nor label present -> not_found', () => {
	const result = resolveMod.resolveSavedOutputDevice(
		{ device_id: 'gone', label: 'USB Audio CODEC' },
		BF1_OUTPUTS
	);
	assert.deepEqual(result, { status: 'not_found' });
});

test('resolver: a label shared by two outputs is ambiguous and never guessed', () => {
	const outputs = [
		{ id: 'usb-1', label: 'USB Audio CODEC' },
		{ id: 'usb-2', label: 'USB Audio CODEC' },
		{ id: 'spk', label: 'Speakers' }
	];
	const result = resolveMod.resolveSavedOutputDevice({ device_id: 'gone', label: 'USB Audio CODEC' }, outputs);
	assert.deepEqual(result, { status: 'ambiguous_label', match_count: 2 });
});

test('resolver: a missing or empty saved label never matches a label-hidden output', () => {
	const hidden = [{ id: 'x', label: '' }];
	assert.deepEqual(resolveMod.resolveSavedOutputDevice({ device_id: 'gone', label: null }, hidden), {
		status: 'not_found'
	});
	assert.deepEqual(resolveMod.resolveSavedOutputDevice({ device_id: 'gone', label: '' }, hidden), {
		status: 'not_found'
	});
});

test('resolver: a malformed saved descriptor fails loud', () => {
	assert.throws(() => resolveMod.resolveSavedOutputDevice({ device_id: '', label: 'x' }, []), TypeError);
	assert.throws(() => resolveMod.resolveSavedOutputDevice({ device_id: 'id', label: 7 }, []), TypeError);
});

//------------------------------------------------------------------ restore harness

function _deck(deckId, stable_id) {
	return {
		deck_id: deckId,
		stable_id,
		source_path: null,
		playing: false,
		position_ms: 0,
		beat_stamp: { kind: 'sample', position_ms: 0 },
		pitch: 1,
		pitch_range: 8,
		master_tempo_enabled: true,
		key_sync_enabled: false,
		quantize_enabled: true,
		beat_sync_enabled: true,
		sync_mode: 'bar',
		is_master: false,
		cue_ms: null,
		loop: null,
		hot_cue_armed: null,
		stems: {
			vocal: { muted: false, solo: false, gain: 0.5 },
			instrumental: { muted: false, solo: false, gain: 0.5 },
			drums: { muted: false, solo: false, gain: 0.5 }
		},
		mixer_channel: {
			trim: 0.5,
			eq_high: 0.5,
			eq_mid: 0.5,
			eq_low: 0.5,
			filter: 0.5,
			fader: 1,
			assign: 'THRU',
			cue_enabled: false
		}
	};
}

function _snapshot(headphones) {
	return snapshotMod.parseRescueSnapshot({
		schema: 1,
		captured_at_ms: 1,
		reason: 'transport',
		app_posture: 'gig',
		master_deck: null,
		playlist_id: null,
		deck_layout: 'more',
		decks: { 1: _deck(1, 'sid-a'), 2: _deck(2, 'sid-b'), 3: _deck(3, 'sid-c'), 4: _deck(4, 'sid-d') },
		mixer: {
			crossfader: 0.5,
			master: 0.8,
			headphones: { mix: 0, level: 0.5, output_mode: 'two_outputs', ...headphones }
		}
	});
}

/**
 * Command log plus a headphone mirror whose transitions are the PRODUCTION
 * functions headphones.ts runs, over the captured labels: a refresh applies
 * reconcileHeadphoneOutputRefresh + dualSinkAssignment (so a speaker-like output
 * IS auto-pinned as master, exactly as on bifrost1), a select is refused by the
 * real assertHeadphoneOutputSelection, and a cue select auto-pins master through
 * the same dualSinkAssignment selectHeadphoneOutput uses. What only a browser
 * can do (enumerateDevices across origins, setSinkId) is covered by
 * tests/live/rescue-output-device-origin.mjs on real hardware.
 */
function _harness({ outputs = BF1_OUTPUTS, refuse = () => null } = {}) {
	const log = [];
	const toasts = [];
	const headphones = { outputs: [], selected_output_device_id: null, selected_master_output_device_id: null };
	const dispatch = async (command) => {
		log.push(command);
		const refusal = refuse(command);
		if (refusal !== null) throw new Error(refusal);
		if (command.type === 'headphone_outputs_refresh') {
			headphones.outputs = outputs;
			const cue = headphonesMod.reconcileHeadphoneOutputRefresh(false, headphones.selected_output_device_id, outputs);
			const assignment = headphonesMod.dualSinkAssignment({
				outputs,
				selectedCueId: cue.selected_output_device_id,
				selectedMasterId: headphones.selected_master_output_device_id
			});
			headphones.selected_output_device_id = assignment.cueId;
			headphones.selected_master_output_device_id = assignment.masterId;
		} else if (command.type === 'headphone_output_select') {
			headphonesMod.assertHeadphoneOutputSelection(command.device_id, headphones.outputs);
			const assignment = headphonesMod.dualSinkAssignment({
				outputs: headphones.outputs,
				selectedCueId: command.device_id,
				selectedMasterId: headphones.selected_master_output_device_id
			});
			headphones.selected_output_device_id = command.device_id;
			if (assignment.autoPinnedMaster) headphones.selected_master_output_device_id = assignment.masterId;
		} else if (command.type === 'headphone_master_select') {
			headphonesMod.assertHeadphoneOutputSelection(command.device_id, headphones.outputs);
			headphones.selected_master_output_device_id = command.device_id;
		}
		return {};
	};
	// duration_ms: a restore that clamps the seek into the loaded track reads it,
	// and without it every deck lands `missing`, hiding the "restore continued" check.
	const idle = { playing: false, audible: false, beatgrid: [], duration_ms: 300_000 };
	const query = () => ({
		decks: { 1: idle, 2: idle, 3: idle, 4: idle },
		mixer: { headphones: { ...headphones } }
	});
	const notify = (message, kind) => toasts.push({ message, kind });
	return { log, toasts, opts: { dispatch, query, notify } };
}

const _selects = (log) =>
	log.filter((c) => c.type === 'headphone_output_select' || c.type === 'headphone_master_select');
const _pauses = (log) => log.filter((c) => c.type === 'play' && c.playing === false);

//------------------------------------------------------------------ restore

test('restore: saved ids that are enumerated are selected by id, silently', async () => {
	const h = _harness();
	const result = await restoreMod.executeRescueRestore(
		{
			snapshot: _snapshot({
				selected_output_device_id: 'b0b000000phones',
				selected_output_device_label: 'Headphones (Realtek(R) Audio)',
				selected_master_output_device_id: 'a11ce0000speakers',
				selected_master_output_device_label: 'Speakers (Realtek(R) Audio)'
			}),
			mode: 'layout'
		},
		h.opts
	);
	assert.deepEqual(_selects(h.log), [
		{ type: 'headphone_output_select', device_id: 'b0b000000phones' },
		{ type: 'headphone_master_select', device_id: 'a11ce0000speakers' }
	]);
	assert.equal(result.sinks.cue.outcome, 'restored');
	assert.equal(result.sinks.master.outcome, 'restored');
	assert.deepEqual(h.toasts, []);
});

test('restore: enumeration is refreshed BEFORE any sink is resolved', async () => {
	const h = _harness();
	await restoreMod.executeRescueRestore(
		{
			snapshot: _snapshot({
				selected_output_device_id: null,
				selected_master_output_device_id: 'a11ce0000speakers',
				selected_master_output_device_label: 'Speakers (Realtek(R) Audio)'
			}),
			mode: 'layout'
		},
		h.opts
	);
	const refreshAt = h.log.findIndex((c) => c.type === 'headphone_outputs_refresh');
	const selectAt = h.log.findIndex((c) => c.type === 'headphone_master_select');
	assert.ok(refreshAt >= 0, 'expected a headphone_outputs_refresh dispatch');
	assert.ok(refreshAt < selectAt, `refresh at ${refreshAt} must precede select at ${selectAt}`);
});

test('restore: salted-away ids are re-found by label and the NEW ids are selected', async () => {
	const h = _harness();
	const result = await restoreMod.executeRescueRestore(
		{
			snapshot: _snapshot({
				selected_output_device_id: 'phones-under-old-origin',
				selected_output_device_label: 'Headphones (Realtek(R) Audio)',
				selected_master_output_device_id: BF1_RING_MASTER_ID,
				selected_master_output_device_label: 'Speakers (Realtek(R) Audio)'
			}),
			mode: 'layout'
		},
		h.opts
	);
	assert.deepEqual(_selects(h.log), [
		{ type: 'headphone_output_select', device_id: 'b0b000000phones' },
		{ type: 'headphone_master_select', device_id: 'a11ce0000speakers' }
	]);
	assert.equal(result.sinks.cue.outcome, 'restored_by_label');
	assert.equal(result.sinks.master.outcome, 'restored_by_label');
	assert.equal(result.sinks.master.device_id, 'a11ce0000speakers');
	assert.deepEqual(h.toasts, []);
});

test('restore: an unresolvable master names the speaker the refresh auto-pinned, not "the default", and the restore continues', async () => {
	const h = _harness();
	const result = await restoreMod.executeRescueRestore(
		{
			snapshot: _snapshot({
				selected_output_device_id: null,
				selected_master_output_device_id: 'gone',
				selected_master_output_device_label: 'USB Audio CODEC'
			}),
			mode: 'layout'
		},
		h.opts
	);
	assert.deepEqual(_selects(h.log), []);
	assert.equal(result.sinks.master.outcome, 'not_found');
	assert.equal(result.sinks.master.device_id, 'a11ce0000speakers', 'the output actually in effect');
	assert.deepEqual(h.toasts, [
		{
			message: "Saved master output 'USB Audio CODEC' not found; using 'Speakers (Realtek(R) Audio)'",
			kind: 'warn'
		}
	]);
	assert.equal(_pauses(h.log).length, 4, 'every loaded deck is still paused after the sink step');
	assert.equal(result.decks['4'].outcome, 'paused');
});

test('restore: with nothing auto-pinned, an unresolvable master says it follows the system default', async () => {
	const outputs = [{ id: 'monitor-1', label: 'LS32D70xE (NVIDIA High Definition Audio)' }];
	const h = _harness({ outputs });
	const result = await restoreMod.executeRescueRestore(
		{
			snapshot: _snapshot({
				selected_output_device_id: null,
				selected_master_output_device_id: 'gone',
				selected_master_output_device_label: 'USB Audio CODEC'
			}),
			mode: 'layout'
		},
		h.opts
	);
	assert.equal(result.sinks.master.outcome, 'not_found');
	assert.equal(result.sinks.master.device_id, null);
	assert.deepEqual(h.toasts, [
		{ message: "Saved master output 'USB Audio CODEC' not found; using the system default output", kind: 'warn' }
	]);
});

test('restore: an ambiguous label is not guessed; the toast names the ambiguity', async () => {
	const outputs = [
		{ id: 'usb-1', label: 'USB Audio CODEC' },
		{ id: 'usb-2', label: 'USB Audio CODEC' }
	];
	const h = _harness({ outputs });
	const result = await restoreMod.executeRescueRestore(
		{
			snapshot: _snapshot({
				selected_output_device_id: 'gone-cue',
				selected_output_device_label: 'USB Audio CODEC',
				selected_master_output_device_id: null
			}),
			mode: 'layout'
		},
		h.opts
	);
	assert.deepEqual(_selects(h.log), []);
	assert.equal(result.sinks.cue.outcome, 'ambiguous');
	assert.deepEqual(h.toasts, [
		{
			message:
				"Saved cue output 'USB Audio CODEC' matches 2 devices; no cue output selected (pick one in I/O)",
			kind: 'warn'
		}
	]);
	assert.equal(_pauses(h.log).length, 4);
});

test('restore: a refused select is reported for THAT sink only; the other sink and the decks still restore', async () => {
	const h = _harness({
		refuse: (c) => (c.type === 'headphone_output_select' ? 'cue setSinkId timed out' : null)
	});
	const result = await restoreMod.executeRescueRestore(
		{
			snapshot: _snapshot({
				selected_output_device_id: 'b0b000000phones',
				selected_output_device_label: 'Headphones (Realtek(R) Audio)',
				selected_master_output_device_id: 'a11ce0000speakers',
				selected_master_output_device_label: 'Speakers (Realtek(R) Audio)'
			}),
			mode: 'layout'
		},
		h.opts
	);
	assert.equal(result.sinks.cue.outcome, 'refused');
	assert.equal(result.sinks.master.outcome, 'restored');
	assert.ok(h.log.some((c) => c.type === 'headphone_master_select'));
	assert.equal(h.toasts.length, 1);
	assert.match(h.toasts[0].message, /^Saved cue output 'Headphones \(Realtek\(R\) Audio\)' could not be restored: cue setSinkId timed out;/);
	assert.equal(_pauses(h.log).length, 4);
});

test('restore: a failed enumeration refuses each saved sink and the restore continues', async () => {
	const h = _harness({
		refuse: (c) => (c.type === 'headphone_outputs_refresh' ? 'enumerateDevices timed out' : null)
	});
	const result = await restoreMod.executeRescueRestore(
		{
			snapshot: _snapshot({
				selected_output_device_id: null,
				selected_master_output_device_id: 'a11ce0000speakers',
				selected_master_output_device_label: 'Speakers (Realtek(R) Audio)'
			}),
			mode: 'layout'
		},
		h.opts
	);
	assert.deepEqual(_selects(h.log), []);
	assert.equal(result.sinks.master.outcome, 'refused');
	assert.equal(result.sinks.cue.outcome, 'none');
	assert.equal(h.toasts.length, 1);
	assert.match(h.toasts[0].message, /enumerateDevices timed out/);
	assert.equal(_pauses(h.log).length, 4);
});

test('restore: a malformed saved sink is refused on its own; the other sink and the decks still restore', async () => {
	const h = _harness();
	const result = await restoreMod.executeRescueRestore(
		{
			snapshot: _snapshot({
				selected_output_device_id: 'gone',
				selected_output_device_label: 7,
				selected_master_output_device_id: 'a11ce0000speakers',
				selected_master_output_device_label: 'Speakers (Realtek(R) Audio)'
			}),
			mode: 'layout'
		},
		h.opts
	);
	assert.equal(result.sinks.cue.outcome, 'refused');
	assert.equal(result.sinks.master.outcome, 'restored');
	assert.equal(h.toasts.length, 1);
	assert.match(h.toasts[0].message, /^Saved cue output \(unnamed, id gone\) could not be restored: saved output label/);
	assert.equal(_pauses(h.log).length, 4);
});

test('restore: the bifrost1 ring (legacy snapshot, id only) no longer aborts the boot', async () => {
	// The harness's master select runs the real assertHeadphoneOutputSelection, the
	// guard whose uncaught refusal aborted the bifrost1 boot.
	const h = _harness();
	const legacy = _snapshot({
		output_mode: 'practice',
		selected_output_device_id: null,
		selected_master_output_device_id: BF1_RING_MASTER_ID
	});
	assert.equal('selected_master_output_device_label' in legacy.mixer.headphones, false);
	const result = await restoreMod.executeRescueRestore({ snapshot: legacy, mode: 'layout' }, h.opts);
	assert.deepEqual(_selects(h.log), [], 'an unresolvable id is never handed to the selector');
	assert.equal(result.sinks.master.outcome, 'not_found');
	assert.deepEqual(h.toasts, [
		{
			message: "Saved master output (unnamed, id c30eed5e) not found; using 'Speakers (Realtek(R) Audio)'",
			kind: 'warn'
		}
	]);
	assert.equal(_pauses(h.log).length, 4);
});

test('restore: no saved sink means no enumeration dispatch and no toast (common path unchanged)', async () => {
	const h = _harness();
	const result = await restoreMod.executeRescueRestore(
		{
			snapshot: _snapshot({ selected_output_device_id: null, selected_master_output_device_id: null }),
			mode: 'layout'
		},
		h.opts
	);
	assert.equal(h.log.some((c) => c.type === 'headphone_outputs_refresh'), false);
	assert.deepEqual(result.sinks, {
		cue: { outcome: 'none', device_id: null, notice: null },
		master: { outcome: 'none', device_id: null, notice: null }
	});
	assert.deepEqual(h.toasts, []);
});

//------------------------------------------------------------------ capture

function _captureState(headphones) {
	const deck = {
		stable_id: null,
		playing: false,
		position_ms: 0,
		beatgrid: [],
		pitch: 1,
		pitch_range: 8,
		master_tempo_enabled: true,
		key_sync_enabled: false,
		quantize_enabled: true,
		beat_sync_enabled: true,
		sync_mode: 'bar',
		is_master: false,
		cue_ms: null,
		loop: null,
		hot_cue_armed: null,
		stems: {
			controls: {
				vocal: { muted: false, solo: false, gain: 0.5 },
				instrumental: { muted: false, solo: false, gain: 0.5 },
				drums: { muted: false, solo: false, gain: 0.5 }
			}
		}
	};
	const channel = {
		trim: 0.5,
		eq_high: 0.5,
		eq_mid: 0.5,
		eq_low: 0.5,
		filter: 0.5,
		fader: 1,
		assign: 'THRU',
		cue_enabled: false
	};
	return {
		master_deck: null,
		browser: { active_playlist: null },
		decks: { 1: deck, 2: deck, 3: deck, 4: deck },
		mixer: {
			crossfader: 0.5,
			master: 0.8,
			channels: { 1: channel, 2: channel, 3: channel, 4: channel },
			headphones: { mix: 0, level: 0.5, output_mode: 'two_outputs', outputs: BF1_OUTPUTS, ...headphones }
		}
	};
}

test('capture: the enumerated label is saved beside each selected sink id', () => {
	const snapshot = snapshotMod.buildRescueSnapshot(
		_captureState({
			selected_output_device_id: 'b0b000000phones',
			selected_master_output_device_id: 'a11ce0000speakers'
		}),
		'transport',
		1,
		{ 1: null, 2: null, 3: null, 4: null }
	);
	assert.equal(snapshot.mixer.headphones.selected_output_device_label, 'Headphones (Realtek(R) Audio)');
	assert.equal(snapshot.mixer.headphones.selected_master_output_device_label, 'Speakers (Realtek(R) Audio)');
});

test('capture: no selection, an unenumerated id, or a hidden label saves a null label', () => {
	const hidden = _captureState({
		outputs: [{ id: 'x', label: '' }],
		selected_output_device_id: 'x',
		selected_master_output_device_id: 'not-listed'
	});
	const snapshot = snapshotMod.buildRescueSnapshot(hidden, 'transport', 1, { 1: null, 2: null, 3: null, 4: null });
	assert.equal(snapshot.mixer.headphones.selected_output_device_label, null);
	assert.equal(snapshot.mixer.headphones.selected_master_output_device_label, null);
	const none = snapshotMod.buildRescueSnapshot(
		_captureState({ selected_output_device_id: null, selected_master_output_device_id: null }),
		'transport',
		1,
		{ 1: null, 2: null, 3: null, 4: null }
	);
	assert.equal(none.mixer.headphones.selected_output_device_label, null);
	assert.equal(none.mixer.headphones.selected_master_output_device_label, null);
});
