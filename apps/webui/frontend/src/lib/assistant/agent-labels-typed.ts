/**
 * Card sentences for OJ's typed tools (apps/engine_core/assistant/oj_tools/).
 * Reads say what OJ is looking up; writes name the real arguments, so the
 * approve card reads as the change it will make. A missing argument reads as
 * "a deck" or "a track", never as blank or "undefined". Pure: no DOM.
 */

export type ToolArgs = Record<string, unknown>;

//----- argument words

const MAX_VALUE_CHARS = 60;

function clip(text: string): string {
	return text.length > MAX_VALUE_CHARS ? `${text.slice(0, MAX_VALUE_CHARS - 3)}...` : text;
}

/** Any argument value as short plain text. */
export function valueText(value: unknown): string {
	if (typeof value === 'string') return clip(value);
	if (value === undefined) return '';
	return clip(JSON.stringify(value));
}

/** A string argument in quotes, or `missing` when it is absent. */
export function quoted(value: unknown, missing: string): string {
	return typeof value === 'string' && value ? `"${clip(value)}"` : missing;
}

/** `noun value`, or `a noun` when the argument is missing. */
export function called(noun: string, value: unknown): string {
	const text = valueText(value);
	return text ? `${noun} ${text}` : `a ${noun}`;
}

/** `deck 2`, or `a deck`. */
export function deck(value: unknown): string {
	return typeof value === 'number' ? `deck ${value}` : 'a deck';
}

/** `track <id>`, or `a track`. */
export function track(value: unknown): string {
	return typeof value === 'string' && value ? `track ${clip(value)}` : 'a track';
}

/** `playlist <id>`, or `a playlist`. */
export function playlist(value: unknown): string {
	return typeof value === 'string' || typeof value === 'number'
		? `playlist ${valueText(value)}`
		: 'a playlist';
}

function counted(value: unknown, many: string, describeOne: (id: unknown) => string): string {
	if (!Array.isArray(value) || value.length === 0) return many;
	return value.length === 1 ? describeOne(value[0]) : `${value.length} ${many}`;
}

/** One track by id, `3 tracks`, or `tracks`. */
export function tracks(value: unknown): string {
	return counted(value, 'tracks', track);
}

function entries(value: unknown): string {
	return counted(value, 'entries', () => '1 entry');
}

function tag(value: unknown): string {
	return typeof value === 'string' && value ? `tag "${clip(value)}"` : 'a tag';
}

function count(value: unknown, noun: string): string {
	return Array.isArray(value) ? ` (${value.length} ${noun})` : '';
}

/** 3600 reads "1 h", 5400 "1 h 30 min", 90 "1 min 30 s". */
export function durationText(seconds: number): string {
	const h = Math.floor(seconds / 3600);
	const m = Math.floor((seconds % 3600) / 60);
	const s = Math.round(seconds % 60);
	const parts = [h ? `${h} h` : '', m ? `${m} min` : '', s ? `${s} s` : ''].filter(Boolean);
	return parts.length ? parts.join(' ') : '0 s';
}

/** 61500 ms reads "1:01.5". */
export function clockText(ms: unknown): string {
	if (typeof ms !== 'number') return 'a position';
	const totalTenths = Math.round(ms / 100);
	const minutes = Math.floor(totalTenths / 600);
	const tenths = totalTenths % 600;
	const whole = String(Math.floor(tenths / 10)).padStart(2, '0');
	const frac = tenths % 10 ? `.${tenths % 10}` : '';
	return `${minutes}:${whole}${frac}`;
}

/** ` from deck 1 to deck 2`, or blank when neither deck is named. */
export function between(a: ToolArgs): string {
	return a.out_deck === undefined && a.in_deck === undefined
		? ''
		: ` from ${deck(a.out_deck)} to ${deck(a.in_deck)}`;
}

/** ` (smooth)` for a named intent, or blank. */
export function intent(a: ToolArgs): string {
	return typeof a.intent === 'string' ? ` (${a.intent})` : '';
}

function searchFilters(a: ToolArgs): string {
	const parts: string[] = [];
	if (typeof a.bpm_min === 'number' || typeof a.bpm_max === 'number') {
		parts.push(`${a.bpm_min ?? 0}-${a.bpm_max ?? 'any'} BPM`);
	}
	if (typeof a.key === 'string') parts.push(`key ${a.key}`);
	if (typeof a.tag === 'string') parts.push(`tag "${a.tag}"`);
	if (typeof a.rating_min === 'number') parts.push(`rating ${a.rating_min}+`);
	return parts.length ? `, ${parts.join(', ')}` : '';
}

function recordingSource(a: ToolArgs): string {
	if (a.source === 'master') return 'the master mix';
	if (a.source === 'none') return 'the tracklist only';
	if (typeof a.device_name === 'string') return `from ${quoted(a.device_name, '')}`;
	return typeof a.source === 'string' ? `from the ${a.source} input` : 'the set';
}

//----- the table

/** One sentence per typed tool, from its call arguments. */
export const TYPED_TOOL_LABEL: Record<string, (a: ToolArgs) => string> = {
	// search.py
	library_search: (a) => `Looking up ${quoted(a.q, 'tracks')} in the library${searchFilters(a)}`,
	track_get: (a) => `Looking up ${track(a.stable_id)}`,
	// playlists.py
	playlist_list: (a) =>
		typeof a.name === 'string'
			? `Looking up playlists named like "${a.name}"`
			: 'Looking up playlists',
	playlist_tracks: (a) => `Looking up the tracks in ${playlist(a.playlist_id)}`,
	playlist_create: (a) => `Create playlist ${quoted(a.name, 'with no name yet')}`,
	playlist_rename: (a) => `Rename ${playlist(a.playlist_id)} to ${quoted(a.name, 'a new name')}`,
	playlist_add_tracks: (a) => {
		const where =
			a.position === 0
				? ' at the start'
				: typeof a.position === 'number'
					? ` at position ${a.position + 1}`
					: '';
		return `Add ${tracks(a.stable_ids)} to ${playlist(a.playlist_id)}${where}`;
	},
	playlist_remove_tracks: (a) => `Remove ${entries(a.item_ids)} from ${playlist(a.playlist_id)}`,
	playlist_reorder: (a) =>
		typeof a.by === 'string'
			? `Sort ${playlist(a.playlist_id)} by ${a.by}${a.descending === true ? ', highest first' : ''}`
			: `Reorder ${playlist(a.playlist_id)}${count(a.item_ids, 'entries')}`,
	// settings.py
	playlist_undo: () => 'Undo the last playlist edit',
	settings_get: (a) =>
		Array.isArray(a.keys) && a.keys.length
			? `Looking up settings: ${a.keys.map(valueText).join(', ')}`
			: 'Looking up your settings',
	ui_pref_set: (a) =>
		`Set preference ${quoted(a.key, 'a preference')} to ${valueText(a.value) || 'a new value'}`,
	// undo.py
	undo_last: () => "Undo OJ's last library edit",
	// sets.py
	set_list: (a) => `Looking up the sets on ${playlist(a.playlist_id)}`,
	set_show: (a) =>
		a.set_id === undefined
			? `Looking up ${playlist(a.playlist_id)} as a set`
			: `Looking up set ${valueText(a.set_id)} on ${playlist(a.playlist_id)}`,
	set_create: (a) => {
		const from =
			typeof a.from_play_order === 'string'
				? ` from play order "${a.from_play_order}"`
				: count(a.parts, 'parts');
		return `Save set ${quoted(a.name, 'with no name yet')} on ${playlist(a.playlist_id)}${from}`;
	},
	set_update_parts: (a) =>
		`Replace the parts of ${called('set', a.set_id)} on ${playlist(a.playlist_id)}${count(a.parts, 'parts')}`,
	set_target: (a) =>
		typeof a.target_s === 'number'
			? `Set the target length of ${playlist(a.playlist_id)} to ${durationText(a.target_s)}`
			: `Clear the target length of ${playlist(a.playlist_id)}`,
	set_skip: (a) =>
		a.skipped === false
			? `Bring ${track(a.stable_id)} back into the set on ${playlist(a.playlist_id)}`
			: `Skip ${track(a.stable_id)} in the set on ${playlist(a.playlist_id)}`,
	// transitions.py
	transition_plan: (a) => `Plan a transition${between(a)}${intent(a)}`,
	transition_start: () => 'Start the planned transition',
	transition_arm_next: (a) => `Arm the next AutoPlay transition${between(a)}${intent(a)}`,
	transition_cancel: () => 'Cancel the armed transition',
	// tags.py
	tag_list: () => 'Looking up your tags',
	tag_create: (a) =>
		`Create tag ${quoted(a.name, 'with no name yet')}${typeof a.type === 'string' ? ` of type ${a.type}` : ''}`,
	tag_assign: (a) => `Tag ${tracks(a.stable_ids)} with ${quoted(a.tag, 'a tag')}`,
	tag_unassign: (a) => `Remove ${tag(a.tag)} from ${tracks(a.stable_ids)}`,
	// smartlists.py
	smartlist_list: () => 'Looking up smartlists',
	smartlist_tracks: (a) => `Looking up the tracks in ${called('smartlist', a.smartlist_id)}`,
	smartlist_create: (a) => `Create smartlist ${quoted(a.name, 'with no name yet')}`,
	smartlist_update: (a) =>
		`Change the rule of ${called('smartlist', a.smartlist_id)}${typeof a.name === 'string' ? ` and rename it to "${a.name}"` : ''}`,
	// hot_cues.py
	hot_cues_list: (a) => `Looking up the hot cues of ${track(a.stable_id)}`,
	hot_cue_set: (a) => {
		const loop = typeof a.out_ms === 'number' ? `, looping to ${clockText(a.out_ms)}` : '';
		return `Set ${called('hot cue', a.slot)} on ${track(a.stable_id)} at ${clockText(a.in_ms)}${loop}`;
	},
	hot_cue_clear: (a) => `Clear ${called('hot cue', a.slot)} on ${track(a.stable_id)}`,
	// recorder.py
	recorder_status: () => 'Looking up the recorder',
	recorder_start: (a) => `Start recording ${recordingSource(a)}`,
	recorder_stop: () => 'Stop recording',
	// stems.py
	stems_state: (a) => `Looking up the stems of ${track(a.stable_id)}`,
	stems_generate: (a) =>
		`Make stems for ${track(a.stable_id)}${typeof a.tier === 'string' ? ` at tier ${a.tier}` : ''}`,
	stems_job: (a) => `Looking up ${called('stems job', a.job_id)}`,
	// analysis.py
	analysis_queue: () => 'Looking up the analysis queue',
	analysis_order: (a) =>
		`Analyse ${typeof a.kind === 'string' ? `the ${a.kind} of ` : ''}${track(a.stable_id)}`
};
