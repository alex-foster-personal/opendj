/**
 * Approve-card sentences for OJ's typed tools (src/lib/assistant/agent-labels-typed.ts via agent-labels.ts).
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let labels;
let typed;
let typed2;

before(async () => {
	labels = await loadTypeScriptModule('src/lib/assistant/agent-labels.ts');
	typed = await loadTypeScriptModule('src/lib/assistant/agent-labels-typed.ts');
	typed2 = await loadTypeScriptModule('src/lib/assistant/agent-labels-typed-2.ts');
});

/** Every typed tool OJ's server modules declare (oj_tools/*.py, one name per ToolSpec). */
const TYPED_TOOLS = [
	'library_search',
	'track_get',
	'playlist_list',
	'playlist_tracks',
	'playlist_create',
	'playlist_rename',
	'playlist_add_tracks',
	'playlist_remove_tracks',
	'playlist_reorder',
	'set_list',
	'set_show',
	'set_create',
	'set_update_parts',
	'set_target',
	'set_skip',
	'transition_plan',
	'transition_start',
	'transition_arm_next',
	'transition_cancel',
	'tag_list',
	'tag_assign',
	'tag_unassign',
	'tag_create',
	'settings_get',
	'ui_pref_set',
	'playlist_undo',
	'undo_last',
	'smartlist_list',
	'smartlist_tracks',
	'smartlist_create',
	'smartlist_update',
	'hot_cues_list',
	'hot_cue_set',
	'hot_cue_clear',
	'recorder_status',
	'recorder_start',
	'recorder_stop',
	'stems_state',
	'stems_generate',
	'stems_job',
	'analysis_queue',
	'analysis_order'
];

const BROKEN_WORDS = /undefined|null|NaN|\[object|\?|  /;

/**
 * Typed tools that do not land, so they get no sentence: OJ keeps its general
 * `command` tool for deck actions, and the deck tools PR (#13812, decks.py) is
 * closed (OJ tools decision, Fri 13 Nov 2026).
 */
const CUT_TOOLS = ['deck_load', 'deck_play', 'deck_pause', 'deck_unload'];

test('a tool whose PR does not land has no typed sentence and reads as its name and arguments', () => {
	/** [if] a cut tool (decks.py, #13812) still has a typed sentence [then] fail, [else stop]. */
	for (const name of CUT_TOOLS) {
		assert.equal(Object.hasOwn(typed.TYPED_TOOL_LABEL, name), false, `${name} is cut`);
	}
	assert.equal(
		labels.toolLabel('deck_load', { deck: 2, stable_id: 'trk-7' }),
		'deck_load (deck: 2, stable_id: trk-7)'
	);
	assert.equal(labels.toolLabel('deck_play', { deck: 1 }), 'deck_play (deck: 1)');
});

test('a playlist_rename call names the playlist and the new name', () => {
	/** [if] a playlist_rename call is held [then] the card names the new name, [else stop]. */
	assert.equal(
		labels.toolLabel('playlist_rename', { playlist_id: 'pl-3', name: 'Warm up', etag: 'e1' }),
		'Rename playlist pl-3 to "Warm up"'
	);
});

test('an unknown tool shows its name and its arguments, never a blank', () => {
	/** [if] an unknown tool name arrives [then] the card shows the name and its arguments, never a blank, [else stop]. */
	assert.equal(
		labels.toolLabel('shell_run', { cmd: 'ls', depth: 2, flags: ['-a'] }),
		'shell_run (cmd: ls, depth: 2, flags: ["-a"])'
	);
	assert.equal(labels.toolLabel('shell_run'), 'shell_run');
	assert.equal(labels.toolLabel('', {}), 'Unnamed tool');
	assert.equal(
		labels.toolLabel('constructor', {}),
		'constructor',
		'prototype names are unknown tools'
	);
});

test('every typed tool has a label that reads cleanly with and without arguments', () => {
	/** [if] any typed tool from the oj_tools modules lacks a label or reads blank or undefined [then] fail, [else stop]. */
	assert.deepEqual(Object.keys(typed.TYPED_TOOL_LABEL).sort(), [...TYPED_TOOLS].sort());
	for (const name of TYPED_TOOLS) {
		const bare = labels.toolLabel(name, {});
		assert.ok(bare.trim().length > 0 && bare !== name, `${name} has its own words`);
		assert.doesNotMatch(bare, BROKEN_WORDS, `${name} reads cleanly with no arguments: ${bare}`);
		assert.equal(labels.toolLabel(name), bare, `${name} without arguments reads as with none`);
	}
});

test('generic tools keep their fixed labels whatever the arguments', () => {
	/** [if] a generic tool's label changes or starts showing arguments [then] fail, [else stop]. */
	assert.equal(labels.toolLabel('library_read', { path: '/api/v1/tracks' }), 'Search library');
	assert.equal(labels.toolLabel('command', { verb: 'play' }), 'Deck command');
	assert.equal(labels.toolLabel('usb_eject'), 'Eject USB stick');
});

test('writes name their real arguments in plain words', () => {
	/** [if] a typed write's card drops the deck, track, playlist, tag or value it changes [then] fail, [else stop]. */
	const cases = [
		[
			'playlist_add_tracks',
			{ playlist_id: 'p', stable_ids: ['a', 'b', 'c'], position: 0 },
			'Add 3 tracks to playlist p at the start'
		],
		[
			'playlist_remove_tracks',
			{ playlist_id: 'p', item_ids: [9] },
			'Remove 1 entry from playlist p'
		],
		[
			'playlist_reorder',
			{ playlist_id: 'p', by: 'bpm', descending: true },
			'Sort playlist p by bpm, highest first'
		],
		['tag_assign', { stable_ids: ['a'], tag: 'Peak' }, 'Tag track a with "Peak"'],
		['tag_unassign', { stable_ids: ['a', 'b'], tag: 'Peak' }, 'Remove tag "Peak" from 2 tracks'],
		['ui_pref_set', { key: 'theme', value: 'dark' }, 'Set preference "theme" to dark'],
		[
			'set_target',
			{ playlist_id: 'p', target_s: 5400 },
			'Set the target length of playlist p to 1 h 30 min'
		],
		['set_target', { playlist_id: 'p', target_s: null }, 'Clear the target length of playlist p'],
		[
			'set_skip',
			{ playlist_id: 'p', stable_id: 'a', skipped: false },
			'Bring track a back into the set on playlist p'
		],
		[
			'transition_plan',
			{ out_deck: 1, in_deck: 2, intent: 'smooth' },
			'Plan a transition from deck 1 to deck 2 (smooth)'
		],
		[
			'hot_cue_set',
			{ stable_id: 'a', slot: 'B', in_ms: 61500, out_ms: 65000 },
			'Set hot cue B on track a at 1:01.5, looping to 1:05'
		],
		[
			'recorder_start',
			{ source: 'external', device_name: 'BlackHole 2ch' },
			'Start recording from "BlackHole 2ch"'
		],
		['stems_generate', { stable_id: 'a', tier: 'M' }, 'Make stems for track a at tier M'],
		['analysis_order', { stable_id: 'a', kind: 'beatgrid' }, 'Analyse the beatgrid of track a']
	];
	for (const [name, args, want] of cases) assert.equal(labels.toolLabel(name, args), want);
});

test('reads say what OJ is looking up', () => {
	/** [if] a typed read's card does not start "Looking up" with its subject [then] fail, [else stop]. */
	assert.equal(
		labels.toolLabel('library_search', { q: 'house', bpm_min: 120, bpm_max: 126, key: '8A' }),
		'Looking up "house" in the library, 120-126 BPM, key 8A'
	);
	assert.equal(labels.toolLabel('track_get', { stable_id: 'a' }), 'Looking up track a');
	assert.equal(labels.toolLabel('settings_get', { keys: ['theme'] }), 'Looking up settings: theme');
});

//----- second batch (agent-labels-typed-2.ts)

/** The typed tools filed after the first batch, by module (oj_tools/<module>.py on their lane branches). */
const SECOND_BATCH = {
	track_edit: ['track_update', 'tracks_bulk_edit'],
	history: ['play_history'],
	similar: ['genre_list', 'tracks_similar'],
	dedup: ['dedup_clusters', 'dedup_decide', 'dedup_apply', 'dedup_undo'],
	title_fix: ['title_fix_proposals', 'title_fix_apply', 'title_fix_reject', 'title_fix_undo'],
	beatgrid: ['beatgrid_flags', 'beatgrid_flag_dismiss'],
	relocate: ['relocate_candidates', 'relocate_apply'],
	pair_transitions: ['pair_transition_get', 'pair_transition_save', 'pair_transition_forget'],
	gain: ['track_gain', 'track_gain_offset_set'],
	export: ['playlist_export_text', 'set_tracklist'],
	cloud_sync: ['cloudsync_status', 'playlist_pin_list', 'playlist_pin_set'],
	deck_sync: ['deck_beat_sync', 'deck_key_shift', 'deck_key_sync', 'deck_master', 'deck_tempo'],
	autoplay: [
		'autoplay_set',
		'autoplay_arm_next',
		'autoplay_decide_now',
		'autoplay_cancel_next',
		'autoplay_decision'
	],
	stem_mix: ['stem_level', 'stem_mute', 'stem_solo'],
	browser: [
		'browser_related',
		'browser_related_criteria',
		'browser_show_playlist',
		'preview_play',
		'preview_stop'
	],
	view: [
		'view_waveform_design',
		'view_waveform_overlay',
		'view_wave_zoom',
		'view_layout_preset',
		'view_skin'
	]
};
const SECOND_TOOLS = Object.values(SECOND_BATCH).flat();

test('a tracks_bulk_edit call for three tracks names the count and the field changed', () => {
	/** [if] a tracks_bulk_edit call for three tracks is held [then] the card says 3 tracks and the field changed, [else stop]. */
	assert.equal(
		labels.toolLabel('tracks_bulk_edit', { stable_ids: ['a', 'b', 'c'], rating: 4 }),
		'Change 3 tracks: rating 4 stars'
	);
	assert.equal(
		labels.toolLabel('tracks_bulk_edit', {
			stable_ids: ['a', 'b', 'c'],
			genre: 'Afro House',
			tags_add: ['Peak']
		}),
		'Change 3 tracks: genre "Afro House", add 1 tag "Peak"'
	);
});

test('a relocate_apply call names the new path', () => {
	/** [if] a relocate_apply call is held [then] the card names the new path, keeping the file name when long, [else stop]. */
	assert.equal(
		labels.toolLabel('relocate_apply', { stable_id: 'a', new_path: '/Volumes/USB/house/a.mp3' }),
		'Relink track a to "/Volumes/USB/house/a.mp3"'
	);
	const long = `/Volumes/USB/${'deep/'.repeat(20)}tune.flac`;
	assert.match(
		labels.toolLabel('relocate_apply', { stable_id: 'a', new_path: long }),
		/^Relink track a to "\.\.\..*tune\.flac"$/
	);
});

test('every second-batch tool has its own label, never the generic fallback', () => {
	/** [if] any second-batch tool name reads as the unknown-tool fallback, blank or undefined [then] fail, [else stop]. */
	assert.deepEqual(Object.keys(typed2.TYPED_TOOL_LABEL_2).sort(), [...SECOND_TOOLS].sort());
	const firstBatch = new Set(Object.keys(typed.TYPED_TOOL_LABEL));
	assert.deepEqual(
		SECOND_TOOLS.filter((name) => firstBatch.has(name)),
		[],
		'no name in both tables'
	);
	for (const name of SECOND_TOOLS) {
		for (const args of [undefined, {}, { deck: 1, stable_id: 'a', value: 0.5 }]) {
			const label = labels.toolLabel(name, args);
			assert.ok(label.trim() && !label.startsWith(name), `${name} has its own words: ${label}`);
			assert.doesNotMatch(label, BROKEN_WORDS, `${name} reads cleanly: ${label}`);
		}
	}
});

/**
 * Second-batch tools that do not land, so they get no sentence: OJ keeps its
 * general `command` tool for actions, and the mixer (#14206, mixer.py), deck
 * loop (#14267, deck_loops.py) and FX (#14295, fx.py) PRs are closed (OJ tools
 * decision, Fri 13 Nov 2026).
 */
const SECOND_CUT = {
	mixer: [
		'mixer_channel_cue',
		'mixer_crossfader',
		'mixer_eq',
		'mixer_fader',
		'mixer_filter',
		'mixer_master_volume',
		'mixer_trim'
	],
	deck_loops: ['deck_beat_loop', 'deck_beat_jump', 'deck_hot_cue_trigger', 'deck_loop_exit'],
	fx: ['fx_beat', 'fx_color']
};

test('a second-batch tool whose PR does not land has no typed sentence', () => {
	/** [if] a cut tool (mixer.py, deck_loops.py, fx.py) still has a typed sentence [then] fail, [else stop]. */
	for (const name of Object.values(SECOND_CUT).flat()) {
		assert.equal(Object.hasOwn(typed2.TYPED_TOOL_LABEL_2, name), false, `${name} is cut`);
		assert.equal(Object.hasOwn(typed.TYPED_TOOL_LABEL, name), false, `${name} is in no table`);
	}
	assert.equal(
		labels.toolLabel('mixer_fader', { deck: 2, value: 0.8 }),
		'mixer_fader (deck: 2, value: 0.8)'
	);
	assert.equal(labels.toolLabel('fx_color', { deck: 'all' }), 'fx_color (deck: all)');
});

test('second-batch writes name their real arguments in plain words', () => {
	/** [if] a second-batch write drops the deck, track, value or target it changes [then] fail, [else stop]. */
	const cases = [
		[
			'track_update',
			{ stable_id: 'a', comments: 'big drop' },
			'Change track a: comment "big drop"'
		],
		[
			'dedup_apply',
			{ cluster_id: 5, survivor: 'a', confirm_cue_loss: true },
			'Merge duplicate group 5 into track a, accepting lost cues'
		],
		[
			'dedup_decide',
			{ cluster_id: 5, action: 'keep-all' },
			'Mark duplicate group 5 to keep every copy'
		],
		['title_fix_apply', { proposal_ids: ['p1', 'p2'] }, 'Apply 2 title fixes'],
		[
			'beatgrid_flag_dismiss',
			{ stable_id: 'a', dismissed: false },
			'Restore the beatgrid flag on track a'
		],
		[
			'pair_transition_save',
			{ plan: { out: { track: 'a' }, in: { track: 'b' } }, style: 'blend' },
			'Save the transition from track a into track b (blend)'
		],
		['track_gain_offset_set', { stable_id: 'a', offset_db: 2 }, 'Set the gain of track a to +2 dB'],
		[
			'track_gain_offset_set',
			{ stable_id: 'a', offset_db: null },
			'Clear your gain offset on track a'
		],
		[
			'playlist_pin_set',
			{ machine_id: 'm1', playlist_id: 'p', mode: 'pinned' },
			'Set playlist p to pinned on machine m1'
		],
		['deck_tempo', { deck: 1, ratio: 1.02 }, 'Set the tempo of deck 1 to +2%'],
		['deck_key_shift', { deck: 2, semitones: -1 }, 'Shift the key of deck 2 by -1 semitone'],
		[
			'deck_key_sync',
			{ deck: 2, enabled: true, target: 1, mode: 'compatible' },
			'Key sync deck 2 to deck 1 (compatible)'
		],
		['deck_master', { deck: 3 }, 'Make deck 3 the master deck'],
		['autoplay_set', { enabled: false }, 'Turn off AutoPlay'],
		['stem_mute', { deck: 1, stem: 'vocal', enabled: true }, 'Mute the vocal stem on deck 1'],
		['stem_level', { deck: 2, stem: 'drums', value: 0.5 }, 'Set the drums stem on deck 2 to 50%'],
		[
			'browser_related_criteria',
			{ bpm_percent: 6, key: true, genre: false },
			'Change what Related tracks match: BPM within 6%, key on, genre off'
		],
		[
			'view_waveform_design',
			{ design: 'rgb', height: 'half' },
			'Draw waveforms in the rgb style at half height'
		],
		[
			'view_waveform_overlay',
			{ overlay: 'hot_cues', enabled: false },
			'Turn off the hot cues waveform overlay'
		],
		['view_wave_zoom', { zoom: 8, deck: 2 }, 'Zoom the waveform on deck 2 to 8 bars'],
		[
			'view_layout_preset',
			{ surface: 'performance', preset: 'replica-desk' },
			'Use the replica-desk layout on the performance screen'
		],
		[
			'view_skin',
			{ skin: 'light', wave_palette: 'blue', wave_split_master: 'on' },
			'Switch to the light skin: blue waveform colours, split master waveform on'
		]
	];
	for (const [name, args, want] of cases) assert.equal(labels.toolLabel(name, args), want);
});

test('second-batch reads say what OJ is looking up', () => {
	/** [if] a second-batch read's card does not start "Looking up" with its subject [then] fail, [else stop]. */
	const cases = [
		['play_history', { limit: 20 }, 'Looking up your play history'],
		['genre_list', { stable_ids: ['a', 'b'] }, 'Looking up the genres of 2 tracks'],
		['tracks_similar', { stable_id: 'a' }, 'Looking up tracks like track a'],
		['title_fix_proposals', { state: 'pending' }, 'Looking up title fixes (pending)'],
		[
			'pair_transition_get',
			{ out: 'a', in: 'b' },
			'Looking up the saved transition from track a into track b'
		],
		['set_tracklist', { session_id: 's1' }, 'Looking up the tracklist of recorded set s1'],
		['autoplay_decision', {}, "Looking up AutoPlay's next pick"]
	];
	for (const [name, args, want] of cases) assert.equal(labels.toolLabel(name, args), want);
});
